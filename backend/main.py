"""FastAPI-бэкенд публичного Mini App: раздаёт фронтенд + JSON API.

Модель: single-user. Любой пользователь Telegram регистрируется при первом входе
(белого списка нет), у каждого свой список и оценки; каталог films — общий,
community-рейтинг = средняя оценка всех пользователей по фильму.

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
import ratelimit
import search
from auth import validate_init_data
from config import BOT_TOKEN

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
    """Проверяем подпись Telegram и регистрируем/обновляем пользователя.
    Белого списка нет — публичный продукт: пускаем любого с валидной подписью."""
    user = validate_init_data(x_init_data, BOT_TOKEN)
    if not user or not user.get("id"):
        raise HTTPException(status_code=401, detail="Не авторизован")
    await db.upsert_user(user)
    return user


# ── API: список пользователя ──────────────────────────────────────────────────
@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return {"id": user["id"], "label": user.get("first_name", ""),
            "username": user.get("username")}


@app.get("/api/movies")
async def movies(status: str = "want_to_watch", sort: str = "date",
                 limit: int = 50, offset: int = 0, user: dict = Depends(current_user)):
    items = await db.get_user_films(user["id"], status, limit=limit, offset=offset, sort=sort)
    return {"items": items, "total": await db.count_user_films(user["id"], status)}


@app.get("/api/movie/{film_id}")
async def movie(film_id: int, user: dict = Depends(current_user)):
    f = await db.get_film(film_id)
    if not f:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    mine = await db.get_user_film(user["id"], film_id)
    f["community"] = await db.community_rating(film_id)
    f["status"] = mine["status"] if mine else None
    f["my_rating"] = mine["rating"] if mine else None
    f["my_comment"] = mine["comment"] if mine else None
    return f


class RateBody(BaseModel):
    rating: int


@app.post("/api/movie/{film_id}/rate")
async def rate(film_id: int, body: RateBody, user: dict = Depends(current_user)):
    if not 1 <= body.rating <= 10:
        raise HTTPException(status_code=422, detail="Оценка 1–10")
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    await db.set_rating(user["id"], film_id, body.rating)  # урок: тап по оценке = «просмотрено»
    logger.info("Rating saved: film=%s user=%s rating=%s", film_id, user["id"], body.rating)
    return {"ok": True}


class StatusBody(BaseModel):
    status: str  # want_to_watch | watched


@app.post("/api/movie/{film_id}/status")
async def set_status(film_id: int, body: StatusBody, user: dict = Depends(current_user)):
    if body.status not in ("want_to_watch", "watched"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    await db.set_status(user["id"], film_id, body.status)
    return {"ok": True}


class CommentBody(BaseModel):
    text: str


@app.post("/api/movie/{film_id}/comment")
async def comment(film_id: int, body: CommentBody, user: dict = Depends(current_user)):
    text = body.text.strip()
    if text:
        await db.set_comment(user["id"], film_id, text[:500])
    else:
        await db.delete_comment(user["id"], film_id)
    return {"ok": True}


@app.delete("/api/movie/{film_id}")
async def delete(film_id: int, user: dict = Depends(current_user)):
    await db.remove_from_list(user["id"], film_id)  # из своего списка; в каталоге остаётся
    return {"ok": True}


# ── API: поиск и добавление ───────────────────────────────────────────────────
@app.get("/api/search")
async def api_search(q: str, user: dict = Depends(current_user)):
    q = q.strip()
    if len(q) < 2:
        return {"items": []}
    # Throttle: один пользователь не должен выжигать общую квоту источника.
    if not ratelimit.allow_user(user["id"]):
        raise HTTPException(status_code=429, detail="Слишком много запросов, подождите минуту")
    imdb_id = search.extract_imdb_id(q)
    if imdb_id:
        d = await search.fetch_details("i", imdb_id)
        return {"items": [d] if d else []}
    res = await search.cached_search(q)  # cache-first + дневной бюджет
    return {"items": res["items"], "limited": res["limited"]}


class AddBody(BaseModel):
    src: str
    ref: str
    status: str = "want_to_watch"


@app.post("/api/add")
async def add(body: AddBody, user: dict = Depends(current_user)):
    details = await search.fetch_details(body.src, body.ref)
    if not details or not details.get("imdb_id"):
        raise HTTPException(status_code=502, detail="Не удалось получить данные")
    film_id = await db.get_or_create_film(**details)  # общий каталог, dedup по imdb_id
    watched_at = datetime.now(timezone.utc).isoformat() if body.status == "watched" else None
    added = await db.add_to_list(user["id"], film_id, body.status, watched_at)
    if not added:
        return {"ok": False, "reason": "exists", "movie_id": film_id}
    return {"ok": True, "movie_id": film_id}


# ── API: статистика (личная) и случайный фильм ────────────────────────────────
@app.get("/api/stats")
async def stats(user: dict = Depends(current_user)):
    s = await db.get_user_stats(user["id"])
    s["year"] = await db.get_year_stats(user["id"], datetime.now(timezone.utc).year)
    return s


@app.get("/api/random")
async def random_movie(user: dict = Depends(current_user)):
    m = await db.get_random_want(user["id"])
    return {"item": m}


# ── API: discovery (публичный каталог) ────────────────────────────────────────
@app.get("/api/browse")
async def browse(sort: str = "popular", genre: str = "", limit: int = 30,
                 offset: int = 0, user: dict = Depends(current_user)):
    limit = max(1, min(limit, 60))
    if sort == "top":
        items = await db.browse_top(user["id"], limit=limit, offset=offset)
    elif sort == "genre":
        if not genre.strip():
            return {"items": []}
        items = await db.browse_by_genre(user["id"], genre.strip(), limit=limit, offset=offset)
    else:
        items = await db.browse_popular(user["id"], limit=limit, offset=offset)
    return {"items": items}


@app.get("/api/genres")
async def genres(user: dict = Depends(current_user)):
    return {"items": await db.list_genres()}


# ── Фронтенд ─────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
