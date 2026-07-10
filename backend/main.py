"""FastAPI-бэкенд Mini App: раздаёт фронтенд + JSON API поверх проверенной логики.

Запуск:  uvicorn main:app --port 8077   (из папки backend/)
"""
import logging
import os
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db
import search
from auth import validate_init_data
from config import ALLOWED_USERS, BOT_TOKEN, USER1_ID, USER2_ID, USER_LABELS

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # урок: иначе лог распухает
logger = logging.getLogger(__name__)

app = FastAPI(title="Movie Mini App")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.on_event("startup")
async def startup() -> None:
    await db.init_db()
    logger.info("Database initialized")


# ── Авторизация: каждый запрос несёт initData в заголовке ────────────────────
async def current_user(x_init_data: str = Header(default="")) -> dict:
    user = validate_init_data(x_init_data, BOT_TOKEN)
    if not user or user.get("id") not in ALLOWED_USERS:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return user


# ── API ───────────────────────────────────────────────────────────────────────
@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return {"id": user["id"], "label": USER_LABELS.get(user["id"], user.get("first_name", ""))}


@app.get("/api/movies")
async def movies(status: str = "want_to_watch", sort: str = "date",
                 limit: int = 50, offset: int = 0, user: dict = Depends(current_user)):
    items = await db.get_movies_by_status(status, limit=limit, offset=offset, sort=sort)
    ratings = await db.get_ratings_bulk([m["id"] for m in items])
    for m in items:
        m["ratings"] = ratings.get(m["id"], {})
    return {"items": items, "total": await db.count_movies(status)}


@app.get("/api/movie/{movie_id}")
async def movie(movie_id: int, user: dict = Depends(current_user)):
    m = await db.get_movie(movie_id)
    if not m:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    m["ratings"] = await db.get_ratings(movie_id)
    m["comments"] = await db.get_comments(movie_id)
    return m


class RateBody(BaseModel):
    rating: int


@app.post("/api/movie/{movie_id}/rate")
async def rate(movie_id: int, body: RateBody, user: dict = Depends(current_user)):
    if not 1 <= body.rating <= 10:
        raise HTTPException(status_code=422, detail="Оценка 1–10")
    m = await db.get_movie(movie_id)
    if not m:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    await db.set_rating(movie_id, user["id"], body.rating)
    if m["status"] != "watched":  # урок: тап по оценке = «просмотрено»
        await db.mark_watched(movie_id)
    logger.info("Rating saved: movie=%s user=%s rating=%s", movie_id, user["id"], body.rating)
    # TODO: уведомить партнёра через бота (sendMessage от BOT_TOKEN).
    return {"ok": True}


class StatusBody(BaseModel):
    status: str  # want_to_watch | watched


@app.post("/api/movie/{movie_id}/status")
async def set_status(movie_id: int, body: StatusBody, user: dict = Depends(current_user)):
    if body.status == "watched":
        await db.mark_watched(movie_id)
    elif body.status == "want_to_watch":
        await db.unmark_watched(movie_id)
    else:
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    return {"ok": True}


class CommentBody(BaseModel):
    text: str


@app.post("/api/movie/{movie_id}/comment")
async def comment(movie_id: int, body: CommentBody, user: dict = Depends(current_user)):
    text = body.text.strip()
    if text:
        await db.set_comment(movie_id, user["id"], text[:500])
    else:
        await db.delete_comment(movie_id, user["id"])
    return {"ok": True}


@app.delete("/api/movie/{movie_id}")
async def delete(movie_id: int, user: dict = Depends(current_user)):
    await db.delete_movie(movie_id)
    return {"ok": True}


@app.get("/api/search")
async def api_search(q: str, user: dict = Depends(current_user)):
    q = q.strip()
    if len(q) < 2:
        return {"items": []}
    imdb_id = search.extract_imdb_id(q)
    if imdb_id:
        d = await search.fetch_details("i", imdb_id)
        return {"items": [d] if d else []}
    return {"items": await search.find_movies(q)}


class AddBody(BaseModel):
    src: str
    ref: str
    status: str = "want_to_watch"


@app.post("/api/add")
async def add(body: AddBody, user: dict = Depends(current_user)):
    details = await search.fetch_details(body.src, body.ref)
    if not details:
        raise HTTPException(status_code=502, detail="Не удалось получить данные")
    existing = await db.get_movie_by_imdb(details["imdb_id"])
    if existing:
        return {"ok": False, "reason": "exists", "movie_id": existing["id"]}
    watched_at = datetime.now(timezone.utc).isoformat() if body.status == "watched" else None
    movie_id = await db.add_movie(added_by=user["id"], status=body.status,
                                  watched_at=watched_at, **details)
    # TODO: уведомить партнёра через бота.
    return {"ok": True, "movie_id": movie_id}


@app.get("/api/stats")
async def stats(user: dict = Depends(current_user)):
    s = await db.get_stats(USER1_ID, USER2_ID)
    s["compatibility"] = await db.get_compatibility(USER1_ID, USER2_ID)
    s["year"] = await db.get_year_stats(datetime.now(timezone.utc).year)
    s["labels"] = {str(k): v for k, v in USER_LABELS.items()}
    return s


@app.get("/api/random")
async def random_movie(user: dict = Depends(current_user)):
    m = await db.get_random_want()
    if not m:
        return {"item": None}
    m["ratings"] = await db.get_ratings(m["id"])
    return {"item": m}


# ── Фронтенд ─────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
