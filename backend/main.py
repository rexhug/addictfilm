"""FastAPI-бэкенд публичного Telegram Mini App для личного кинотрекера."""
import asyncio
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import database as db
import kinopoisk
import omdb
import search
import wikidata
from auth import validate_init_data
from config import BOT_TOKEN, BOT_USERNAME

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = FastAPI(title="Кинотрекер")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


def _user_label(user: dict) -> str:
    return user.get("first_name") or user.get("username") or "Киноман"


def _public_user(user: dict | None) -> dict | None:
    if not user:
        return None
    return {"id": user["id"], "label": _user_label(user)}


@app.on_event("startup")
async def startup() -> None:
    await db.init_db()
    logger.info("База данных инициализирована")


@app.on_event("shutdown")
async def shutdown() -> None:
    """Закрывает HTTP-сессии внешних источников при остановке приложения."""
    await asyncio.gather(kinopoisk.aclose(), omdb.aclose(), wikidata.aclose(), db.close_db())


# ── Авторизация ──────────────────────────────────────────────────────────────
async def current_user(x_init_data: str = Header(default="")) -> dict:
    telegram_user = validate_init_data(x_init_data, BOT_TOKEN)
    if not telegram_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return await db.upsert_user(telegram_user)


# ── Личный кинотрекер ─────────────────────────────────────────────────────────
@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    partner = await db.get_partner(user["id"])
    return {**_public_user(user), "partner": _public_user(partner)}


@app.get("/api/movies")
async def movies(
    status: Literal["want_to_watch", "watched", "top"] = "want_to_watch",
    sort: Literal["date", "rating"] = "date",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(current_user),
):
    items = await db.get_movies_by_status(user["id"], status, limit=limit, offset=offset, sort=sort)
    return {"items": items, "total": await db.count_movies(user["id"], status)}


@app.get("/api/movie/{movie_id}")
async def movie(movie_id: int, user: dict = Depends(current_user)):
    item = await db.get_movie(user["id"], movie_id)
    if not item:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    item["ratings"] = await db.get_ratings(movie_id, user["id"])
    item["comments"] = await db.get_comments(movie_id, user["id"])
    return item


class RateBody(BaseModel):
    rating: int = Field(ge=1, le=10)


@app.post("/api/movie/{movie_id}/rate")
async def rate(movie_id: int, body: RateBody, user: dict = Depends(current_user)):
    item = await db.get_movie(user["id"], movie_id)
    if not item:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    await db.set_rating(movie_id, user["id"], body.rating)
    if item["status"] != "watched":
        await db.mark_watched(user["id"], movie_id)
    logger.info("Оценка сохранена: movie=%s user=%s rating=%s", movie_id, user["id"], body.rating)
    return {"ok": True}


class StatusBody(BaseModel):
    status: Literal["want_to_watch", "watched"]


@app.post("/api/movie/{movie_id}/status")
async def set_status(movie_id: int, body: StatusBody, user: dict = Depends(current_user)):
    if not await db.get_movie(user["id"], movie_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    if body.status == "watched":
        await db.mark_watched(user["id"], movie_id)
    else:
        await db.unmark_watched(user["id"], movie_id)
    return {"ok": True}


class CommentBody(BaseModel):
    text: str = Field(max_length=500)


@app.post("/api/movie/{movie_id}/comment")
async def comment(movie_id: int, body: CommentBody, user: dict = Depends(current_user)):
    if not await db.get_movie(user["id"], movie_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    text = body.text.strip()
    if text:
        await db.set_comment(movie_id, user["id"], text)
    else:
        await db.delete_comment(movie_id, user["id"])
    return {"ok": True}


@app.delete("/api/movie/{movie_id}")
async def delete(movie_id: int, user: dict = Depends(current_user)):
    if not await db.get_movie(user["id"], movie_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    await db.delete_user_movie(user["id"], movie_id)
    return {"ok": True}


@app.get("/api/search")
async def api_search(q: str = Query(max_length=200), user: dict = Depends(current_user)):
    q = q.strip()
    if len(q) < 2:
        return {"items": []}
    imdb_id = search.extract_imdb_id(q)
    if imdb_id:
        details = await search.fetch_details("i", imdb_id)
        return {"items": [details] if details else []}
    return {"items": await search.find_movies(q)}


class AddBody(BaseModel):
    src: Literal["k", "i"]
    ref: str = Field(min_length=1, max_length=64)
    status: Literal["want_to_watch", "watched"] = "want_to_watch"


@app.post("/api/add")
async def add(body: AddBody, user: dict = Depends(current_user)):
    details = await search.fetch_details(body.src, body.ref)
    if not details:
        raise HTTPException(status_code=502, detail="Не удалось получить данные фильма")
    movie_id = await db.get_or_create_movie(**details)
    added = await db.add_user_movie(user["id"], movie_id, body.status)
    if not added:
        return {"ok": False, "reason": "exists", "movie_id": movie_id}
    return {"ok": True, "movie_id": movie_id}


@app.get("/api/stats")
async def stats(user: dict = Depends(current_user)):
    personal = await db.get_personal_stats(user["id"])
    partner = await db.get_partner(user["id"])
    pair = await db.get_pair_stats(user["id"], partner["id"]) if partner else None
    return {
        "personal": personal,
        "year": await db.get_year_stats(user["id"], datetime.now(timezone.utc).year),
        "partner": _public_user(partner),
        "pair": pair,
    }


@app.get("/api/random")
async def random_movie(user: dict = Depends(current_user)):
    item = await db.get_random_want(user["id"])
    if not item:
        return {"item": None}
    item["ratings"] = await db.get_ratings(item["id"], user["id"])
    return {"item": item}


# ── Партнёр ───────────────────────────────────────────────────────────────────
@app.get("/api/partner")
async def partner(user: dict = Depends(current_user)):
    return {"partner": _public_user(await db.get_partner(user["id"]))}


@app.post("/api/partner/invite")
async def create_partner_invite(user: dict = Depends(current_user)):
    token = secrets.token_urlsafe(18)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    invite = await db.create_partner_invite(user["id"], token, expires_at)
    if not invite:
        raise HTTPException(status_code=409, detail="Сначала отключи текущую пару")
    link = f"https://t.me/{BOT_USERNAME}?startapp=pair_{token}" if BOT_USERNAME else None
    return {**invite, "link": link}


class PartnerAcceptBody(BaseModel):
    token: str = Field(min_length=12, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


@app.post("/api/partner/accept")
async def accept_partner_invite(body: PartnerAcceptBody, user: dict = Depends(current_user)):
    result, partner_user = await db.accept_partner_invite(body.token, user["id"])
    errors = {
        "not_found": (404, "Приглашение не найдено"),
        "already_used": (409, "Приглашение уже использовано"),
        "expired": (410, "Срок действия приглашения истёк"),
        "self": (422, "Нельзя принять собственное приглашение"),
        "already_paired": (409, "У одного из вас уже есть партнёр"),
    }
    if result != "accepted":
        status_code, detail = errors[result]
        raise HTTPException(status_code=status_code, detail=detail)
    return {"ok": True, "partner": _public_user(partner_user)}


@app.delete("/api/partner")
async def disconnect_partner(user: dict = Depends(current_user)):
    if not await db.remove_partner(user["id"]):
        raise HTTPException(status_code=404, detail="Партнёр не подключён")
    return {"ok": True}


# ── Фронтенд ─────────────────────────────────────────────────────────────────
@app.get("/healthz", include_in_schema=False)
async def healthcheck():
    """Публичная проверка готовности для Railway без доступа к пользовательским данным."""
    await db.healthcheck()
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
