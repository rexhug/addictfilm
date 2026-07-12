"""FastAPI-бэкенд публичного Mini App: раздаёт фронтенд + JSON API.

Модель: single-user. Любой пользователь Telegram регистрируется при первом входе
(белого списка нет), у каждого свой список и оценки; каталог films — общий,
community-рейтинг = средняя оценка всех пользователей по фильму.

Запуск:  uvicorn main:app --port 8077   (из папки backend/)
"""
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db
import kinopoisk
import omdb
import posters
import ratelimit
import search
from auth import validate_init_data
from config import ADMIN_TOKEN, BOT_TOKEN

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # урок: иначе лог распухает
logger = logging.getLogger(__name__)

app = FastAPI(title="Movie Mini App")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.on_event("startup")
async def startup() -> None:
    await db.init_db()
    await search.purge_expired()  # подчистить протухший кэш поиска при старте
    logger.info("Database initialized")


@app.on_event("shutdown")
async def shutdown() -> None:
    for mod in (kinopoisk, omdb):
        try:
            await mod.aclose()
        except Exception:  # noqa: BLE001
            pass
    global _img_session
    if _img_session and not _img_session.closed:
        await _img_session.close()


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
    if status not in ("want_to_watch", "watched", "top"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    limit = max(1, min(limit, 100))   # защита от чрезмерной выборки
    offset = max(0, offset)
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
    await db.sync_film_to_partner(user["id"], film_id)  # пара: партнёру фильм в «Хочу»
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
    await db.sync_film_to_partner(user["id"], film_id)  # пара: партнёру фильм в «Хочу»
    return {"ok": True}


class CommentBody(BaseModel):
    text: str


@app.post("/api/movie/{film_id}/comment")
async def comment(film_id: int, body: CommentBody, user: dict = Depends(current_user)):
    if not await db.get_film(film_id):  # иначе set_comment создаёт «сиротский» user_films
        raise HTTPException(status_code=404, detail="Фильм не найден")
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
    imdb_id = search.extract_imdb_id(q)
    if imdb_id:  # прямой запрос по tt-id идёт в OMDb → тоже под throttle
        if not ratelimit.allow_user(user["id"]):
            raise HTTPException(status_code=429, detail="Слишком много запросов, подождите минуту")
        d = await search.fetch_details("i", imdb_id)
        return {"items": [d] if d else []}
    # Throttle считается внутри — только если реально идём в API (кэш-хиты бесплатны).
    res = await search.cached_search(q, user["id"])
    if res["throttled"]:
        raise HTTPException(status_code=429, detail="Слишком много запросов, подождите минуту")
    return {"items": res["items"], "limited": res["limited"]}


class AddBody(BaseModel):
    src: str
    ref: str
    status: str = "want_to_watch"


@app.post("/api/add")
async def add(body: AddBody, user: dict = Depends(current_user)):
    if body.src not in ("k", "i"):
        raise HTTPException(status_code=422, detail="Неизвестный источник")
    if body.status not in ("want_to_watch", "watched"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    details = await search.fetch_details(body.src, body.ref)
    if not details or not details.get("imdb_id"):
        raise HTTPException(status_code=502, detail="Не удалось получить данные")
    film_id = await db.get_or_create_film(**details)  # общий каталог, dedup по imdb_id
    watched_at = datetime.now(timezone.utc).isoformat() if body.status == "watched" else None
    added = await db.add_to_list(user["id"], film_id, body.status, watched_at)
    await db.sync_film_to_partner(user["id"], film_id)  # пара: партнёру фильм в «Хочу»
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


# ── API: пара (партнёрство) ───────────────────────────────────────────────────
BOT_USERNAME = os.getenv("BOT_USERNAME", "addictfilmbot")


def _invite_link(token: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?startapp=inv_{token}"


def _partner_brief(u: dict | None) -> dict:
    return {"id": u["id"], "name": u.get("first_name") or ""} if u else {"id": None, "name": ""}


@app.get("/api/partner")
async def partner(user: dict = Depends(current_user)):
    pid = await db.get_partner(user["id"])
    if pid is not None:
        return {"status": "paired", "partner": _partner_brief(await db.get_user(pid))}
    token = await db.get_pending_invite(user["id"])
    if token:
        return {"status": "invited", "link": _invite_link(token), "code": token}
    return {"status": "none"}


@app.post("/api/partner/invite")
async def partner_invite(user: dict = Depends(current_user)):
    if await db.get_partner(user["id"]) is not None:
        raise HTTPException(status_code=409, detail="Пара уже есть")
    token = await db.create_invite(user["id"])
    return {"link": _invite_link(token), "code": token}


class AcceptBody(BaseModel):
    token: str


@app.post("/api/partner/accept")
async def partner_accept(body: AcceptBody, user: dict = Depends(current_user)):
    token = body.token.strip()
    if token.startswith("inv_"):
        token = token[4:]
    res = await db.accept_invite(token, user["id"])
    if not res["ok"]:
        return {"ok": False, "reason": res["reason"]}
    return {"ok": True, "partner": _partner_brief(await db.get_user(res["partner_id"]))}


@app.post("/api/partner/unpair")
async def partner_unpair(user: dict = Depends(current_user)):
    await db.unpair(user["id"])
    return {"ok": True}


@app.get("/api/partner/stats")
async def partner_stats(user: dict = Depends(current_user)):
    pair = await db.get_pair(user["id"])
    if pair is None:
        raise HTTPException(status_code=404, detail="Нет пары")
    s = await db.pair_period_stats(user["id"], pair["partner_id"], pair["since"])
    s["partner"] = _partner_brief(await db.get_user(pair["partner_id"]))
    return s


# ── Обслуживание (админ по ADMIN_TOKEN) ──────────────────────────────────────
def require_admin(x_admin_token: str = Header(default="")) -> None:
    """Гейт для служебных эндпоинтов. Без заданного ADMIN_TOKEN — выключены (404)."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Не авторизован")


@app.post("/api/admin/backfill-posters", dependencies=[Depends(require_admin)])
async def backfill_posters(limit: int = 200, omdb_cap: int = 60):
    """Добрать постеры фильмам без картинки (kinopoisk → OMDb). Идемпотентно;
    вызывать повторно, пока remaining не станет 0."""
    return await posters.backfill(limit=limit, _omdb_cap=omdb_cap)


# ── Прокси постеров (обходит блокировку CDN на стороне клиента) ───────────────
# Картинки грузятся через наш домен, а не напрямую с Amazon/Yandex — работает
# везде, где открывается само приложение. Без авторизации (тег <img> её не шлёт).
_ALLOWED_IMG_HOSTS = {
    "m.media-amazon.com", "images-na.ssl-images-amazon.com", "ia.media-imdb.com",
    "avatars.mds.yandex.net", "st.kp.yandex.net", "image.openmoviedb.com",
    "imagetmdb.com", "kinopoiskapiunofficial.tech",
}
_img_session: aiohttp.ClientSession | None = None


async def _img_sess() -> aiohttp.ClientSession:
    global _img_session
    if _img_session is None or _img_session.closed:
        _img_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12), headers={"User-Agent": "Mozilla/5.0"})
    return _img_session


@app.get("/img")
async def img_proxy(u: str):
    p = urlparse(u)
    if p.scheme not in ("http", "https") or p.netloc.lower() not in _ALLOWED_IMG_HOSTS:
        raise HTTPException(status_code=400, detail="Недопустимый источник")  # анти-SSRF
    try:
        async with (await _img_sess()).get(u) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=404, detail="Постер не найден")
            data = await resp.read()
            ctype = resp.headers.get("Content-Type", "image/jpeg")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="Не удалось загрузить постер")
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ── Фронтенд ─────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    # no-store: HTML всегда свежий, чтобы новые версии app.js/style.css (?v=) подхватывались.
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"),
                        headers={"Cache-Control": "no-store, max-age=0"})

app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
