"""FastAPI-бэкенд публичного Mini App: раздаёт фронтенд + JSON API.

Модель: single-user. Любой пользователь Telegram регистрируется при первом входе
(белого списка нет), у каждого свой список и оценки; каталог films — общий,
community-рейтинг = средняя оценка всех пользователей по фильму.

Запуск:  uvicorn main:app --port 8077   (из папки backend/)
"""
import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import aiohttp
import sentry_sdk
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import database as db
import db_runtime
import kinopoisk
import omdb
import posters
import ratelimit
import search
import stats_cache
from auth import validate_init_data
from config import ADMIN_TOKEN, ADMIN_USER_IDS, BOT_TOKEN, DATABASE_URL, SENTRY_DSN

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # урок: иначе лог распухает
logger = logging.getLogger(__name__)

if SENTRY_DSN:
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.0, send_default_pii=False)

app = FastAPI(title="Movie Mini App")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    """Даёт в Fly/Sentry реальное время медленных запросов без новых сервисов."""
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if elapsed_ms >= 750:
        logger.warning("Slow request: %s %s -> %s in %.0fms",
                       request.method, request.url.path, response.status_code, elapsed_ms)
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.0f}"
    return response

# Фоновий щоденний бекап SQLite (Postgres робить бекапи сам — backup_db там no-op).
_backup_task: asyncio.Task | None = None


async def _periodic_backup() -> None:
    """Раз на добу робити VACUUM INTO-бекап SQLite. При помилці — лог, не падати."""
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            path = await db.backup_db()
            if path:
                logger.info("Scheduled SQLite backup: %s", path)
        except Exception:  # noqa: BLE001
            logger.warning("Scheduled backup failed", exc_info=True)

@app.on_event("startup")
async def startup() -> None:
    await db_runtime.start(DATABASE_URL)  # пул Postgres; для SQLite — no-op
    await db.init_db()
    await search.purge_expired()  # подчистить протухший кэш поиска при старте
    # SQLite требует прикладного бэкапа; PostgreSQL обслуживается провайдером.
    global _backup_task
    if not DATABASE_URL and (_backup_task is None or _backup_task.done()):
        _backup_task = asyncio.create_task(_periodic_backup(), name="sqlite-periodic-backup")
    logger.info("Database initialized (%s)", "Postgres" if DATABASE_URL else "SQLite")


@app.on_event("shutdown")
async def shutdown() -> None:
    global _backup_task
    if _backup_task is not None:
        _backup_task.cancel()
        try:
            await _backup_task
        except asyncio.CancelledError:
            pass
        _backup_task = None
    for mod in (kinopoisk, omdb):
        try:
            await mod.aclose()
        except Exception:  # noqa: BLE001
            pass
    await db_runtime.close()
    global _img_session
    if _img_session and not _img_session.closed:
        await _img_session.close()


# ── Авторизация: каждый запрос несёт initData в заголовке ────────────────────
async def current_user(x_init_data: str = Header(default="")) -> dict:
    """Проверяем подпись Telegram и регистрируем/обновляем пользователя.
    Белого списка нет — публичный продукт: пускаем любого с валидной подписью."""
    user = validate_init_data(x_init_data, BOT_TOKEN)
    user_id = user.get("id") if user else None
    if isinstance(user_id, bool):
        user_id = None
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        user_id = None
    if not user or not user_id or user_id < 0:
        raise HTTPException(status_code=401, detail="Не авторизован")
    user["id"] = user_id
    await db.upsert_user(user)
    return user


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Readiness для Fly: процесс и активная БД должны быть доступны."""
    try:
        if not await db.ping():
            raise RuntimeError("empty database ping response")
    except Exception:  # noqa: BLE001
        logger.exception("Health check failed: database is unavailable")
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"ok": True}


async def _effective_role(user_id: int) -> str | None:
    """"admin" — если id в ADMIN_USER_IDS (bootstrap-секрет, всегда есть, не зависит
    от БД); иначе — роль из users.role ("editor"/"admin", назначается вручную)."""
    if user_id in ADMIN_USER_IDS:
        return "admin"
    return await db.get_user_role(user_id)


async def require_editor(user: dict = Depends(current_user)) -> dict:
    """Гейт для in-app админки подборок — по самому Telegram-юзеру (не по токену,
    как require_admin ниже — тот для curl/скриптов обслуживания)."""
    role = await _effective_role(user["id"])
    if role not in ("editor", "admin"):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


# ── API: список пользователя ──────────────────────────────────────────────────
@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return {"id": user["id"], "label": user.get("first_name", ""),
            "username": user.get("username"), "photo_url": user.get("photo_url"),
            "role": await _effective_role(user["id"])}


@app.get("/api/movies")
async def movies(status: str = "want_to_watch", sort: str = "date",
                 limit: int = 50, offset: int = 0, user: dict = Depends(current_user)):
    if status not in ("want_to_watch", "watched", "top"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    if sort not in ("date", "rating"):
        raise HTTPException(status_code=422, detail="Неизвестная сортировка")
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
    f["share_link"] = _movie_link(film_id)
    return f


class RateBody(BaseModel):
    rating: int = Field(ge=1, le=10)


@app.post("/api/movie/{film_id}/rate")
async def rate(film_id: int, body: RateBody, user: dict = Depends(current_user)):
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    await db.set_rating(user["id"], film_id, body.rating)  # урок: тап по оценке = «просмотрено»
    await db.sync_film_to_partner(user["id"], film_id)  # пара: партнёру фильм в «Хочу»
    stats_cache.clear()
    logger.info("Rating saved: film=%s user=%s rating=%s", film_id, user["id"], body.rating)
    return {"ok": True}


@app.delete("/api/movie/{film_id}/rate")
async def unrate(film_id: int, user: dict = Depends(current_user)):
    """Убрать оценку — повторный тап по своей звезде. Статус (списки) не меняется."""
    await db.clear_rating(user["id"], film_id)
    stats_cache.clear()
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
    stats_cache.clear()
    return {"ok": True}


class CommentBody(BaseModel):
    text: str = Field(max_length=500)


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
    stats_cache.clear()
    return {"ok": True}


# ── API: поиск и добавление ───────────────────────────────────────────────────
@app.get("/api/search")
async def api_search(q: str, user: dict = Depends(current_user)):
    q = q.strip()
    if len(q) > 200:
        raise HTTPException(status_code=422, detail="Слишком длинный поисковый запрос")
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


async def _resolve_film_id(src: str, ref: str) -> int:
    """Дедуп до внешних API + fetch_details + get_or_create_film — общий путь для
    /api/add и /api/admin/collections/{id}/films. Для src="i" ref == imdb_id, и если
    фильм уже в общем каталоге — линкуем сразу, не тратя лимит kinopoisk/OMDb."""
    ref = ref.strip()
    if src == "k" and not re.fullmatch(r"\d{1,12}", ref):
        raise HTTPException(status_code=422, detail="Некорректный идентификатор фильма")
    if src == "i" and not re.fullmatch(r"tt\d{5,12}", ref):
        raise HTTPException(status_code=422, detail="Некорректный IMDb идентификатор")

    film_id = None
    if src == "i":
        film_id = await db.get_film_id_by_imdb(ref)
    if film_id is None:
        details = await search.fetch_details(src, ref)
        if not details or not details.get("imdb_id"):
            raise HTTPException(status_code=502, detail="Не удалось получить данные")
        film_id = await db.get_or_create_film(**details)  # общий каталог, dedup по imdb_id
    return film_id


class AddBody(BaseModel):
    src: str
    ref: str = Field(max_length=128)
    status: str = "want_to_watch"


@app.post("/api/add")
async def add(body: AddBody, user: dict = Depends(current_user)):
    if body.src not in ("k", "i"):
        raise HTTPException(status_code=422, detail="Неизвестный источник")
    if body.status not in ("want_to_watch", "watched"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    film_id = await _resolve_film_id(body.src, body.ref)
    watched_at = datetime.now(timezone.utc).isoformat() if body.status == "watched" else None
    added = await db.add_to_list(user["id"], film_id, body.status, watched_at)
    await db.sync_film_to_partner(user["id"], film_id)  # пара: партнёру фильм в «Хочу»
    if added:
        stats_cache.clear()
    if not added:
        return {"ok": False, "reason": "exists", "movie_id": film_id}
    return {"ok": True, "movie_id": film_id}


# ── API: статистика (личная) и случайный фильм ────────────────────────────────
@app.get("/api/stats")
async def stats(user: dict = Depends(current_user)):
    year = datetime.now(timezone.utc).year
    key = ("personal", user["id"], year)
    cached = stats_cache.get(key)
    if cached is not None:
        return cached
    s = await db.get_user_stats(user["id"])
    s["year"] = await db.get_year_stats(user["id"], year)
    return stats_cache.put(key, s)


@app.get("/api/random")
async def random_movie(user: dict = Depends(current_user)):
    m = await db.get_random_want(user["id"])
    return {"item": m}


# ── API: discovery (публичный каталог) ────────────────────────────────────────
@app.get("/api/browse")
async def browse(sort: str = "popular", genre: str = "", limit: int = 30,
                 offset: int = 0, user: dict = Depends(current_user)):
    limit = max(1, min(limit, 60))
    offset = max(0, offset)
    if sort not in ("popular", "top", "genre"):
        raise HTTPException(status_code=422, detail="Неизвестная сортировка")
    if sort == "top":
        items = await db.browse_top(user["id"], limit=limit, offset=offset)
    elif sort == "genre":
        if not genre.strip():
            return {"items": []}
        if len(genre.strip()) > 80:
            raise HTTPException(status_code=422, detail="Слишком длинный жанр")
        items = await db.browse_by_genre(user["id"], genre.strip(), limit=limit, offset=offset)
    else:
        items = await db.browse_popular(user["id"], limit=limit, offset=offset)
    return {"items": items}


@app.get("/api/genres")
async def genres(user: dict = Depends(current_user)):
    return {"items": await db.list_genres()}


# ── API: подборки (кураторские коллекции — публичный просмотр + in-app админка) ─
@app.get("/api/collections")
async def collections_list(user: dict = Depends(current_user)):
    return {"items": await db.list_collections()}


@app.get("/api/collections/{collection_id}")
async def collection_detail(collection_id: int, user: dict = Depends(current_user)):
    c = await db.get_collection(collection_id)
    if not c:
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    c["items"] = await db.get_collection_films(collection_id, user["id"])
    return c


class CollectionBody(BaseModel):
    title: str = Field(max_length=500)


@app.post("/api/admin/collections", dependencies=[Depends(require_editor)])
async def collection_create(body: CollectionBody, user: dict = Depends(current_user)):
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Пустое название")
    return {"id": await db.create_collection(title[:80], user["id"])}


@app.delete("/api/admin/collections/{collection_id}", dependencies=[Depends(require_editor)])
async def collection_delete(collection_id: int):
    await db.delete_collection(collection_id)
    return {"ok": True}


class CollectionAddBody(BaseModel):
    src: str
    ref: str = Field(max_length=128)


@app.post("/api/admin/collections/{collection_id}/films", dependencies=[Depends(require_editor)])
async def collection_add_film(collection_id: int, body: CollectionAddBody):
    if body.src not in ("k", "i"):
        raise HTTPException(status_code=422, detail="Неизвестный источник")
    if not await db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    film_id = await _resolve_film_id(body.src, body.ref)
    added = await db.add_film_to_collection(collection_id, film_id)
    return {"ok": True, "added": added, "movie_id": film_id}


@app.delete("/api/admin/collections/{collection_id}/films/{film_id}",
            dependencies=[Depends(require_editor)])
async def collection_remove_film(collection_id: int, film_id: int):
    await db.remove_film_from_collection(collection_id, film_id)
    return {"ok": True}


# ── API: пара (партнёрство) ───────────────────────────────────────────────────
BOT_USERNAME = os.getenv("BOT_USERNAME", "addictfilmbot")


def _invite_link(token: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?startapp=inv_{token}"


def _movie_link(film_id: int) -> str:
    """Диплинк на конкретный фильм (startapp) — для кнопки «Поделиться»."""
    return f"https://t.me/{BOT_USERNAME}?startapp=film_{film_id}"


def _partner_brief(u: dict | None) -> dict:
    if not u:
        return {"id": None, "name": "", "username": None, "photo_url": None, "avatar_url": None}
    photo_url = u.get("photo_url")
    return {"id": u["id"], "name": u.get("first_name") or "", "username": u.get("username"),
            "photo_url": photo_url,
            "avatar_url": None if photo_url else _avatar_url(u["id"])}


def _avatar_signature(user_id: int) -> str:
    return hmac.new(BOT_TOKEN.encode(), f"avatar:{user_id}".encode(), hashlib.sha256).hexdigest()[:40]


def _avatar_url(user_id: int) -> str | None:
    """Opaque, token-free URL for a partner avatar fetched through our backend."""
    if not BOT_TOKEN:
        return None
    return f"/api/avatar/{user_id}?sig={_avatar_signature(user_id)}"


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
    token: str = Field(max_length=128)


@app.post("/api/partner/accept")
async def partner_accept(body: AcceptBody, user: dict = Depends(current_user)):
    token = body.token.strip()
    if token.startswith("inv_"):
        token = token[4:]
    res = await db.accept_invite(token, user["id"])
    if not res["ok"]:
        return {"ok": False, "reason": res["reason"]}
    stats_cache.clear()
    return {"ok": True, "partner": _partner_brief(await db.get_user(res["partner_id"]))}


@app.post("/api/partner/unpair")
async def partner_unpair(user: dict = Depends(current_user)):
    await db.unpair(user["id"])
    stats_cache.clear()
    return {"ok": True}


@app.get("/api/partner/stats")
async def partner_stats(user: dict = Depends(current_user)):
    pair = await db.get_pair(user["id"])
    if pair is None:
        raise HTTPException(status_code=404, detail="Нет пары")
    key = ("pair", user["id"], pair["partner_id"], pair["since"])
    s = stats_cache.get(key)
    if s is None:
        s = await db.pair_period_stats(user["id"], pair["partner_id"], pair["since"])
        s = stats_cache.put(key, s)
    s["partner"] = _partner_brief(await db.get_user(pair["partner_id"]))
    return s


# ── Обслуживание (админ по ADMIN_TOKEN) ──────────────────────────────────────
def require_admin(x_admin_token: str = Header(default="")) -> None:
    """Гейт для служебных эндпоинтов. Без заданного ADMIN_TOKEN — выключены (404)."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")
    if not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Не авторизован")


@app.post("/api/admin/backfill-posters", dependencies=[Depends(require_admin)])
async def backfill_posters(limit: int = 200, omdb_cap: int = 60):
    """Добрать постеры фильмам без картинки (kinopoisk → OMDb). Идемпотентно;
    вызывать повторно, пока remaining не станет 0."""
    return await posters.backfill(limit=max(1, min(limit, 500)), _omdb_cap=max(1, min(omdb_cap, 200)))


@app.post("/api/admin/upgrade-omdb-posters", dependencies=[Depends(require_admin)])
async def upgrade_omdb_posters(limit: int = 200, name_cap: int = 60):
    """Заменить постеры Amazon/OMDb на kinopoisk-версии у уже добавленных фильмов.
    Идемпотентно; вызывать повторно, пока kept_omdb не перестанет уменьшаться."""
    return await posters.upgrade_omdb_posters(limit=max(1, min(limit, 500)), _name_cap=max(1, min(name_cap, 200)))


# ── Прокси постеров (обходит блокировку CDN на стороне клиента) ───────────────
# Картинки грузятся через наш домен, а не напрямую с Amazon/Yandex — работает
# везде, где открывается само приложение. Без авторизации (тег <img> её не шлёт).
_ALLOWED_IMG_HOSTS = {
    "m.media-amazon.com", "images-na.ssl-images-amazon.com", "ia.media-imdb.com",
    "avatars.mds.yandex.net", "st.kp.yandex.net", "image.openmoviedb.com",
    "imagetmdb.com", "kinopoiskapiunofficial.tech",
}
_ALLOWED_IMG_TYPES = {"image/avif", "image/gif", "image/jpeg", "image/png", "image/webp"}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_REDIRECTS = 3
_img_session: aiohttp.ClientSession | None = None


async def _img_sess() -> aiohttp.ClientSession:
    global _img_session
    if _img_session is None or _img_session.closed:
        _img_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15, connect=5), headers={"User-Agent": "Mozilla/5.0"})
    return _img_session


@app.get("/api/avatar/{user_id}", include_in_schema=False)
async def telegram_avatar(user_id: int, sig: str = ""):
    """Проксі аватара партнера без передачі Telegram bot token у браузер.

    Telegram не додає photo_url партнера в initData поточного користувача, тому
    для старих профілів добираємо останнє фото через Bot API. Посилання підписане
    серверним токеном і не дозволяє довільно використовувати цей endpoint.
    """
    if user_id <= 0 or not BOT_TOKEN or not hmac.compare_digest(sig, _avatar_signature(user_id)):
        raise HTTPException(status_code=404, detail="Аватар не знайдено")
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos"
    try:
        async with (await _img_sess()).get(api_url, params={"user_id": user_id, "limit": 1}) as resp:
            payload = await resp.json(content_type=None)
        photos = payload.get("result", {}).get("photos", []) if payload.get("ok") else []
        if not photos:
            raise HTTPException(status_code=404, detail="Аватар не знайдено")
        file_id = photos[0][-1].get("file_id")
        if not file_id:
            raise HTTPException(status_code=404, detail="Аватар не знайдено")
        file_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
        async with (await _img_sess()).get(file_url, params={"file_id": file_id}) as resp:
            file_payload = await resp.json(content_type=None)
        file_path = file_payload.get("result", {}).get("file_path") if file_payload.get("ok") else None
        if not file_path:
            raise HTTPException(status_code=404, detail="Аватар не знайдено")
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        async with (await _img_sess()).get(download_url) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=404, detail="Аватар не знайдено")
            data = await _read_image_limited(resp.content)
        ctype = _img_ctype(data) or "image/jpeg"
        return Response(content=data, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=3600"})
    except HTTPException:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
        raise HTTPException(status_code=404, detail="Аватар не знайдено")


# Дисковый кэш картинок на томе /data (пустует после миграции БД на Postgres).
# Смысл: раздача с локального диска стабильнее и быстрее, чем каждый раз ходить
# на CDN Яндекса/Amazon — меньше шансов оборвать медленное мобильное соединение.
_IMG_CACHE_DIR = os.getenv("IMG_CACHE_DIR") or ("/data/imgcache" if os.path.isdir("/data") else "")


def _img_ctype(data: bytes) -> str | None:
    """Content-Type по магическим байтам (в кэше храним только тело)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:12] == b"ftypavif":
        return "image/avif"
    return None


def _img_cache_path(u: str) -> str | None:
    if not _IMG_CACHE_DIR:
        return None
    key = hashlib.sha256(u.encode()).hexdigest()
    return os.path.join(_IMG_CACHE_DIR, key[:2], key)


def _is_allowed_image_url(url: str) -> bool:
    """Проверить URL до каждого запроса, включая промежуточные редиректы."""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() in _ALLOWED_IMG_HOSTS


async def _read_image_limited(content: aiohttp.StreamReader) -> bytes:
    """Прочитать поток целиком и остановиться раньше лимита памяти."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Изображение слишком большое")
        chunks.append(chunk)
    return b"".join(chunks)


@app.get("/img")
async def img_proxy(u: str):
    if len(u) > 4096:
        raise HTTPException(status_code=400, detail="Недопустимый источник")
    if not _is_allowed_image_url(u):
        raise HTTPException(status_code=400, detail="Недопустимый источник")  # анти-SSRF

    cache_path = _img_cache_path(u)
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            data = f.read()
        cached_ctype = _img_ctype(data)
        if cached_ctype:
            return Response(content=data, media_type=cached_ctype,
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # Две попытки к CDN. Редиректы проходим вручную: каждая цель повторно
    # проверяется по allowlist, чтобы не превратить прокси в SSRF-канал.
    data = None
    ctype = None
    for attempt in (1, 2):
        try:
            current_url = u
            for _ in range(_MAX_IMAGE_REDIRECTS + 1):
                async with (await _img_sess()).get(current_url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            raise HTTPException(status_code=404, detail="Изображение не найдено")
                        current_url = urljoin(current_url, location)
                        if not _is_allowed_image_url(current_url):
                            raise HTTPException(status_code=400, detail="Недопустимый источник")
                        continue
                    if resp.status != 200:
                        raise HTTPException(status_code=404, detail="Изображение не найдено")
                    ctype = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if ctype not in _ALLOWED_IMG_TYPES:
                        raise HTTPException(status_code=415, detail="Неподдерживаемый формат изображения")
                    if resp.content_length is not None and resp.content_length > _MAX_IMAGE_BYTES:
                        raise HTTPException(status_code=413, detail="Изображение слишком большое")
                    data = await _read_image_limited(resp.content)
                    detected_ctype = _img_ctype(data)
                    if not detected_ctype:
                        raise HTTPException(status_code=415, detail="Неподдерживаемый формат изображения")
                    ctype = detected_ctype
                    break
            if data is not None:
                break
            raise HTTPException(status_code=502, detail="Слишком много перенаправлений")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise HTTPException(status_code=502, detail="Не удалось загрузить изображение")

    if cache_path and data:
        try:  # атомарная запись: tmp + rename (второй инстанс может писать параллельно)
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            tmp = f"{cache_path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, cache_path)
        except OSError:
            pass  # кэш — оптимизация, не роняем отдачу из-за диска

    return Response(content=data, media_type=ctype or _img_ctype(data),
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ── Фронтенд ─────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    # no-store: HTML всегда свежий, чтобы новые версии app.js/style.css (?v=) подхватывались.
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"),
                        headers={"Cache-Control": "no-store, max-age=0"})

class VersionedStaticFiles(StaticFiles):
    """Довго кешує лише versioned JS/CSS; HTML завжди віддає endpoint вище."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in {"app.js", "style.css"} and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.mount("/", VersionedStaticFiles(directory=FRONTEND_DIR), name="static")
