"""FastAPI-бэкенд публичного Mini App: раздаёт фронтенд + JSON API.

Модель: single-user. Любой пользователь Telegram регистрируется при первом входе
(белого списка нет), у каждого свой список и оценки; каталог films — общий,
community-рейтинг = средняя оценка всех пользователей по фильму.

Запуск:  uvicorn main:app --port 8077   (из папки backend/)
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urljoin, urlparse

import aiohttp
import cast as cast_model
import database as db
import db_runtime
import fanart
import hero_media
import kinopoisk
import omdb
import pair_activity_notifications
import pair_notifications
import posters
import ratelimit
import recommendations
import search
import sentry_sdk
import stats_cache
import user_touch
import wikidata
from auth import extract_start_param, validate_init_data
from config import (
    ADMIN_TOKEN,
    ADMIN_USER_IDS,
    BOT_TOKEN,
    CAST_V2_ENABLED,
    DATABASE_URL,
    FANART_HERO_ENABLED,
    FULLSCREEN_SINGLE_PICK_ENABLED,
    KINOPOISK_HERO_ENABLED,
    SENTRY_DSN,
    SENTRY_TRACES_SAMPLE_RATE,
    WISHLIST_PREPARE_SECRET,
)
from enrichment.cast_backfill import enrich_film_cast
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from observability import RequestMetrics, observe_request
from pydantic import BaseModel, Field
from recommendation import engines
from recommendation_questions import next_question_id, public_question
from starlette.middleware.gzip import GZipMiddleware

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # урок: иначе лог распухает
logger = logging.getLogger(__name__)

if SENTRY_DSN:
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
                    send_default_pii=False)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """FastAPI 0.139 deprecates the on_event hooks.  Bodies stay below, next to
    the code they touch; they resolve at call time, so definition order does
    not matter.
    """
    await _startup()
    try:
        yield
    finally:
        # `finally`, not a bare call: the shutdown hook used to be skipped when
        # startup raised, leaking the aiohttp sessions and the Postgres pool.
        await _shutdown()


app = FastAPI(title="Movie Mini App", lifespan=lifespan)
# Fly не стискає ці файли за нас. GZip значно зменшує 95KB JS і 50KB CSS на
# першому відкритті Mini App; already-compressed image responses не чіпає.
app.add_middleware(GZipMiddleware, minimum_size=512, compresslevel=5)
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
_HTML_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    # Все картинки идут через собственный /img-прокси с allowlist хостов, а
    # единственный внешний ресурс — data:-текстура фона. Разрешать https:
    # целиком значит держать открытым канал для трекинг-пикселя на случай,
    # если XSS когда-нибудь появится.
    # Аватары Telegram в отзывах и профиле. Именованные хосты, а не открытый
    # https: при XSS трекинг-пиксель можно было бы поставить куда угодно, а сюда
    # — только к Telegram, который этого человека и так видит.
    #
    # Хостов два, и это не перестраховка: t.me отдаёт 302 на cdnN.telesco.pe
    # (замер по нашим пользователям — cdn1 и cdn4), а браузер проверяет img-src
    # ЗАНОВО на цели редиректа. Проверено в Chromium: с одним t.me аватар
    # блокируется. Через собственный /img их не пустить по той же причине —
    # в анти-SSRF списке пришлось бы разрешать целый суффикс.
    "img-src 'self' data: https://t.me https://*.telesco.pe; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors https://*.telegram.org"
)


_REQUEST_METRICS_MAX_SAMPLES = max(50, int(os.getenv("REQUEST_METRICS_MAX_SAMPLES", "500")))
_request_metrics = RequestMetrics(_REQUEST_METRICS_MAX_SAMPLES)


def _performance_snapshot() -> dict:
    return _request_metrics.snapshot()

# Фоновий щоденний бекап SQLite (Postgres робить бекапи сам — backup_db там no-op).
_backup_task: asyncio.Task | None = None
_pair_expiry_task: asyncio.Task | None = None
_visual_enrichment_tasks: set[asyncio.Task] = set()
_visual_enrichment_film_ids: set[int] = set()
_people_enrichment_tasks: set[asyncio.Task] = set()
_people_enrichment_film_ids: set[int] = set()
_profile_portrait_lookup_gate = asyncio.BoundedSemaphore(2)
_PROFILE_PORTRAIT_FILM_LIMIT = 40
_PROFILE_PORTRAIT_BATCH_SIZE = 20
_director_profile_enrichment_tasks: set[asyncio.Task] = set()
_director_profile_enrichment_users: set[int] = set()


@app.middleware("http")
async def database_request_scope(request: Request, call_next):
    async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
        return await call_next(request)


@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    """Give Fly/Sentry real latency and keep private API responses uncacheable."""
    return await observe_request(_request_metrics, request, call_next, logger)


async def _periodic_backup() -> None:
    """Раз на добу робити VACUUM INTO-бекап SQLite. При помилці — лог, не падати."""
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            path = await db.backup_db()
            if path:
                logger.info("Scheduled SQLite backup: %s", path)
        except Exception:
            logger.warning("Scheduled backup failed", exc_info=True)


async def _emit_expired_pair_invites() -> None:
    for invite in await db.expire_pending_invites():
        await pair_notifications.dispatch_pair_event(event=invite["event"])


async def _periodic_pair_expiry() -> None:
    """A small hourly sweep; the database transition makes it safe on many Fly instances."""
    while True:
        await asyncio.sleep(3600)
        try:
            await pair_notifications.replay_pending_pair_events()
            await pair_notifications.retry_notification_deliveries()
            await _emit_expired_pair_invites()
        except Exception:
            logger.warning("Pair invite expiry sweep failed", exc_info=True)

async def _startup() -> None:
    await db_runtime.start(DATABASE_URL)  # пул Postgres; для SQLite — no-op
    await db.init_db()
    # Events and their audience are committed together with the pair state.
    # Replaying unacknowledged rows makes an interrupted HTTP request safe.
    await pair_notifications.replay_pending_pair_events()
    # Events may already be acknowledged while their Telegram delivery is
    # waiting on a temporary outage.  Recover those durable rows separately.
    await pair_notifications.retry_notification_deliveries()
    # Expiry is durable and idempotent; only newly transitioned rows are
    # returned.  Old links never remain actionable after a restart/deploy.
    await _emit_expired_pair_invites()
    await search.purge_expired()  # подчистить протухший кэш поиска при старте
    # История подбора росла линейно от активности и не чистилась вовсе.
    # «Не предлагать» остаётся навсегда, всё остальное живёт 90 дней.
    removed = await db.prune_recommendation_history()
    if removed:
        logger.info("Подчищено записей истории подбора: %s", removed)
    # SQLite требует прикладного бэкапа; PostgreSQL обслуживается провайдером.
    global _backup_task, _pair_expiry_task
    if not DATABASE_URL and (_backup_task is None or _backup_task.done()):
        _backup_task = db_runtime.create_background_task(_periodic_backup(), name="sqlite-periodic-backup")
    if _pair_expiry_task is None or _pair_expiry_task.done():
        _pair_expiry_task = db_runtime.create_background_task(_periodic_pair_expiry(), name="pair-invite-expiry")
    logger.info("Database initialized (%s)", "Postgres" if DATABASE_URL else "SQLite")


async def _shutdown() -> None:
    global _backup_task, _pair_expiry_task
    if _backup_task is not None:
        _backup_task.cancel()
        try:
            await _backup_task
        except asyncio.CancelledError:
            pass
        _backup_task = None
    if _pair_expiry_task is not None:
        _pair_expiry_task.cancel()
        try:
            await _pair_expiry_task
        except asyncio.CancelledError:
            pass
        _pair_expiry_task = None
    for task in list(_visual_enrichment_tasks):
        task.cancel()
    if _visual_enrichment_tasks:
        await asyncio.gather(*_visual_enrichment_tasks, return_exceptions=True)
    _visual_enrichment_tasks.clear()
    _visual_enrichment_film_ids.clear()
    for task in list(_people_enrichment_tasks):
        task.cancel()
    if _people_enrichment_tasks:
        await asyncio.gather(*_people_enrichment_tasks, return_exceptions=True)
    _people_enrichment_tasks.clear()
    _people_enrichment_film_ids.clear()
    # Третий набор фоновых задач гасился не так, как два предыдущих: на SIGTERM
    # во время деплоя дообогащение портретов обрывалось на полуслове.
    for task in list(_director_profile_enrichment_tasks):
        task.cancel()
    if _director_profile_enrichment_tasks:
        await asyncio.gather(*_director_profile_enrichment_tasks, return_exceptions=True)
    _director_profile_enrichment_tasks.clear()
    _director_profile_enrichment_users.clear()
    for mod in (kinopoisk, omdb, wikidata):
        try:
            await mod.aclose()
        except Exception:
            pass
    await pair_notifications.drain()
    await db_runtime.close()
    global _img_session
    if _img_session and not _img_session.closed:
        await _img_session.close()
    global _img_trim_task
    if _img_trim_task is not None:
        try:
            await _img_trim_task
        except asyncio.CancelledError:
            pass
        _img_trim_task = None


# Legacy names: the shutdown path is covered by test_resource_hygiene, and a
# refactor is not a reason to rewrite the test that guards it.
startup = _startup
shutdown = _shutdown


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
    # Параметр берём из той же строки, подпись которой только что проверена, и
    # передаём отдельным аргументом: в профиль он не подмешивается.
    if user_touch.should_persist(user):
        await db.upsert_user(user, start_param=extract_start_param(x_init_data))
    return user


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Readiness для Fly: процесс и активная БД должны быть доступны."""
    try:
        if not await db.ping():
            raise RuntimeError("empty database ping response")
    except Exception:
        logger.exception("Health check failed: database is unavailable")
        # from None: наружу уходит только код 503 — детали БД в ответе не нужны.
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    return {"ok": True}


async def _effective_role(user_id: int) -> str | None:
    """"admin" — если id в ADMIN_USER_IDS (bootstrap-секрет, всегда есть, не зависит
    от БД); иначе — роль из users.role ("editor"/"admin", назначается вручную)."""
    if user_id in ADMIN_USER_IDS:
        return "admin"
    return await db.get_user_role(user_id)


async def throttled_mutation(user: dict = Depends(current_user)) -> dict:
    """429 instead of an unbounded write path.  Applied at the boundary, not
    inside handlers, so a new write endpoint is guarded by adding one Depends.
    """
    if not ratelimit.allow_mutation(user["id"]):
        raise HTTPException(status_code=429, detail="Слишком часто, попробуйте через минуту")
    return user


async def throttled_quiz(user: dict = Depends(current_user)) -> dict:
    """Quiz calls INSERT a session row each, so they get their own budget."""
    if not ratelimit.allow_quiz(user["id"]):
        raise HTTPException(status_code=429, detail="Слишком часто, попробуйте через минуту")
    return user


async def require_editor(user: dict = Depends(current_user)) -> dict:
    """Гейт для in-app админки подборок — по самому Telegram-юзеру (не по токену,
    как require_admin ниже — тот для curl/скриптов обслуживания)."""
    role = await _effective_role(user["id"])
    if role not in ("editor", "admin"):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


async def require_admin_user(user: dict = Depends(current_user)) -> dict:
    """Строже require_editor: только роль "admin".

    Редактор ведёт подборки, и знать размер и источники аудитории ему для этого
    не нужно. Отдельный гейт, а не проверка внутри обработчика: иначе следующий
    аналитический эндпоинт легко повесят на editor по невнимательности.
    """
    if await _effective_role(user["id"]) != "admin":
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


# Возможности текущего пользователя. Единственный источник правды для фронта о
# том, показывать ли админский раздел. Обычному пользователю отдаём пустой
# набор — по ответу нельзя узнать, кто вообще является администратором.
_ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "editor": ("collections.read", "collections.write", "content.publish"),
    "admin": ("collections.read", "collections.write", "content.publish", "audit.read"),
}


@app.get("/api/me/capabilities")
async def me_capabilities(user: dict = Depends(current_user)):
    role = await _effective_role(user["id"])
    capabilities = _ROLE_CAPABILITIES.get(role or "", ())
    return {"is_admin": bool(capabilities), "admin_role": role if capabilities else None,
            "capabilities": list(capabilities)}


def _client_features() -> dict:
    """Только про представление. Версии движка подбора сюда не попадают: они
    приходят из СЕССИИ и только администратору (см. _admin_quiz_engine_meta)."""
    return {
        "fullscreen_single_pick": FULLSCREEN_SINGLE_PICK_ENABLED,
        "cast_v2": CAST_V2_ENABLED,
    }


# ── API: список пользователя ──────────────────────────────────────────────────
@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    settings = await db.get_notification_settings(user["id"])
    return {"id": user["id"], "label": user.get("first_name", ""),
            "username": user.get("username"), "photo_url": user.get("photo_url"),
            "role": await _effective_role(user["id"]), "telegram_available": bool(BOT_TOKEN),
            # Состояние интерфейсных возможностей приходит с сервера один раз, на
            # старте. Собирать его на клиенте из своей сборки нельзя: закешированный
            # Telegram-ом фронтенд пережил бы откат флага и рисовал бы экран,
            # которого на сервере уже нет.
            "features": _client_features(), **settings}


class SettingsBody(BaseModel):
    language: str | None = Field(default=None, max_length=8)
    telegram_notifications: bool | None = None


@app.get("/api/settings")
async def get_settings(user: dict = Depends(current_user)):
    return {**(await db.get_notification_settings(user["id"])), "telegram_available": bool(BOT_TOKEN)}


@app.patch("/api/settings")
async def patch_settings(body: SettingsBody, user: dict = Depends(current_user)):
    if body.language is not None and body.language not in ("ru", "en"):
        raise HTTPException(status_code=422, detail="Unsupported language")
    settings = await db.update_notification_settings(
        user["id"], language=body.language, telegram_enabled=body.telegram_notifications)
    return {**settings, "telegram_available": bool(BOT_TOKEN)}


@app.get("/api/notifications")
async def notifications(limit: int = 20, before_id: int | None = None,
                        category: Literal["all", "pair", "films", "system"] = "all",
                        user: dict = Depends(current_user)):
    return await db.list_notifications(
        user["id"], limit=limit, before_id=before_id, category=category)


@app.post("/api/notifications/read-all")
async def notifications_read_all(user: dict = Depends(current_user)):
    return {"updated": await db.mark_all_notifications_read(user["id"])}


@app.post("/api/notifications/{notification_id}/read")
async def notification_read(notification_id: int, user: dict = Depends(current_user)):
    return {"ok": await db.mark_notification_read(user["id"], notification_id)}


@app.get("/api/movies")
async def movies(status: str = "want_to_watch", sort: str = "date",
                 limit: int = 50, offset: int = 0, user: dict = Depends(current_user)):
    if status not in ("want_to_watch", "watched", "top"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    if sort not in ("date", "rating", "best", "worst", "new", "old"):
        raise HTTPException(status_code=422, detail="Неизвестная сортировка")
    limit = max(1, min(limit, 100))   # защита от чрезмерной выборки
    offset = max(0, offset)
    items, total = await asyncio.gather(
        db.get_user_films(user["id"], status, limit=limit, offset=offset, sort=sort),
        db.count_user_films(user["id"], status),
    )
    return {"items": items, "total": total}


@app.get("/api/movie/{film_id}")
async def movie(film_id: int, user: dict = Depends(current_user)):
    f = await db.get_film(film_id)
    if not f:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    # Visual enrichment must never sit on the critical path for opening a film.
    # The current card returns from the catalog right away; a background task
    # improves missing poster/backdrop for the next view.
    imdb_id = f.get("imdb_id")
    needs_poster = not f.get("poster_url") and not f.get("poster_checked_at")
    needs_backdrop = not f.get("backdrop_url") and not f.get("artwork_checked_at")
    if (needs_poster or needs_backdrop) and imdb_id and imdb_id.startswith("tt"):
        _schedule_visual_enrichment(film_id, imdb_id)
    needs_actor_photos = cast_model.cast_refresh_due(f)
    needs_director_photos = bool(f.get("directors")) and not f.get("director_photos_checked_at")
    if (needs_actor_photos or needs_director_photos) and (
        f.get("kp_id") or (imdb_id and imdb_id.startswith("tt"))
    ):
        _schedule_people_enrichment(
            film_id, imdb_id or "", needs_actor_photos, needs_director_photos,
        )
    context, f["community"] = await asyncio.gather(
        db.get_movie_rating_context(user["id"], film_id), db.community_rating(film_id))
    mine = context["mine"]
    f["status"] = mine["status"] if mine else None
    f["my_rating"] = mine["rating"] if mine else None
    f["my_comment"] = mine["comment"] if mine else None
    f["my_comment_status"] = mine["comment_status"] if mine else None
    f["my_commented_at"] = mine["commented_at"] if mine else None
    f["cast"] = db.decode_film_cast(f.get("cast_json"))
    f["partner"] = context["partner"]
    f["share_link"] = _movie_link(film_id)
    return f


async def _enrich_film_visuals(film_id: int, imdb_id: str) -> None:
    """Fill missing visual assets outside the user-facing movie-detail request."""
    try:
        assets = (await kinopoisk.assets_by_imdb([imdb_id])).get(imdb_id)
        if assets is not None:
            await db.mark_film_visuals_checked(
                imdb_id, assets.get("poster_url"), assets.get("backdrop_url"),
                assets.get("age_rating"),
            )
    except Exception:
        logger.warning("Visual enrichment failed for film %s", film_id, exc_info=True)
    finally:
        _visual_enrichment_film_ids.discard(film_id)


def _schedule_visual_enrichment(film_id: int, imdb_id: str) -> None:
    """Schedule at most one in-process visual lookup per film."""
    if film_id in _visual_enrichment_film_ids:
        return
    _visual_enrichment_film_ids.add(film_id)
    task = db_runtime.create_background_task(_enrich_film_visuals(film_id, imdb_id),
                                             name=f"film-visuals-{film_id}")
    _visual_enrichment_tasks.add(task)
    task.add_done_callback(_visual_enrichment_tasks.discard)


async def _enrich_film_people(film_id: int, imdb_id: str, enrich_actors: bool = True,
                              enrich_directors: bool = True) -> bool:
    """Research cast/director portraits outside the user-facing request."""
    try:
        film = await db.get_film(film_id)
        if not film:
            return False
        director_map = (
            await wikidata.get_directors_by_imdb([imdb_id])
            if enrich_directors and imdb_id.startswith("tt") else {}
        )
        enriched = False
        if enrich_actors:
            outcome = await enrich_film_cast(
                film, validate_portraits=True, person_detail_limit=3,
            )
            enriched = outcome.wrote or enriched
        if enrich_directors:
            directors = director_map.get(imdb_id, [])
            if directors:
                names = ", ".join(person["name"] for person in directors)
                await db.set_film_directors_from_wikidata(
                    film_id, names, json.dumps(directors, ensure_ascii=False),
                )
                enriched = True
            else:
                await db.mark_film_director_photos_checked(film_id)
        return enriched
    except Exception:
        logger.warning("People enrichment failed for film %s", film_id, exc_info=True)
        return False
    finally:
        _people_enrichment_film_ids.discard(film_id)


async def _empty_people_map() -> dict:
    return {}


def _schedule_people_enrichment(film_id: int, imdb_id: str, enrich_actors: bool = True,
                                enrich_directors: bool = True) -> None:
    """Schedule free people lookup per film, never on the UI path."""
    if film_id in _people_enrichment_film_ids:
        return
    _people_enrichment_film_ids.add(film_id)
    task = db_runtime.create_background_task(
        _enrich_film_people(film_id, imdb_id, enrich_actors, enrich_directors),
        name=f"film-people-{film_id}",
    )
    _people_enrichment_tasks.add(task)
    task.add_done_callback(_people_enrichment_tasks.discard)


class RateBody(BaseModel):
    rating: int = Field(ge=1, le=10)


@app.post("/api/movie/{film_id}/rate")
async def rate(film_id: int, body: RateBody, user: dict = Depends(throttled_mutation)):
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    result = await db.set_rating(
        user["id"], film_id, body.rating,
    )  # урок: тап по оценке = «просмотрено»
    await db.sync_film_to_partner(user["id"], film_id)  # пара: партнёру фильм в «Хочу»
    await _invalidate_stats_for(user["id"])
    try:
        await pair_activity_notifications.notify_partner_film_rated(
            actor_id=user["id"],
            film_id=film_id,
            rating=body.rating,
            first_rating=result.first_rating,
        )
    except Exception:
        # The rating is the primary product action. Inbox materialization uses
        # only local database work and must never turn a saved score into 5xx.
        logger.warning(
            "Partner rating notification failed: actor=%s film=%s",
            user["id"], film_id, exc_info=True,
        )
    logger.info("Rating saved: film=%s user=%s rating=%s", film_id, user["id"], body.rating)
    return {"ok": True}


@app.delete("/api/movie/{film_id}/rate")
async def unrate(film_id: int, user: dict = Depends(current_user)):
    """Убрать оценку — повторный тап по своей звезде. Статус (списки) не меняется."""
    await db.clear_rating(user["id"], film_id)
    await _invalidate_stats_for(user["id"])
    return {"ok": True}


class StatusBody(BaseModel):
    status: str  # want_to_watch | watched


@app.post("/api/movie/{film_id}/status")
async def set_status(film_id: int, body: StatusBody, user: dict = Depends(throttled_mutation)):
    if body.status not in ("want_to_watch", "watched"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    await db.set_status(user["id"], film_id, body.status)
    await db.sync_film_to_partner(user["id"], film_id)  # пара: партнёру фильм в «Хочу»
    await _invalidate_stats_for(user["id"])
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


class ReviewBody(BaseModel):
    rating: int = Field(ge=1, le=10)
    text: str = Field(min_length=1, max_length=500)


class LegacyReviewBody(BaseModel):
    action: Literal["publish", "keep_private", "delete"]


class ReviewReportBody(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


def _guard_review_write(user_id: int) -> None:
    if not ratelimit.allow_review_write(user_id):
        raise HTTPException(status_code=429, detail="Слишком много изменений. Попробуйте через минуту")


@app.get("/api/movie/{film_id}/reviews")
async def movie_reviews(film_id: int, limit: int = 10, before_id: int | None = None,
                        sort: Literal["newest", "highest", "lowest"] = "newest",
                        user: dict = Depends(current_user)):
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    try:
        return await db.list_movie_reviews(
            user["id"], film_id, limit=max(1, min(limit, 50)),
            before_id=before_id, sort=sort,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.put("/api/movie/{film_id}/review")
async def publish_movie_review(film_id: int, body: ReviewBody,
                               user: dict = Depends(current_user)):
    _guard_review_write(user["id"])
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Напишите текст отзыва")
    item = await db.set_public_review(user["id"], film_id, body.rating, text)
    await _invalidate_stats_for(user["id"])
    return {"ok": True, "item": item}


@app.delete("/api/movie/{film_id}/review")
async def delete_movie_review(film_id: int, user: dict = Depends(current_user)):
    _guard_review_write(user["id"])
    deleted = await db.delete_public_review(user["id"], film_id)
    return {"ok": True, "deleted": deleted}


@app.post("/api/movie/{film_id}/review/legacy")
async def legacy_movie_review(film_id: int, body: LegacyReviewBody,
                              user: dict = Depends(current_user)):
    _guard_review_write(user["id"])
    if body.action == "keep_private":
        return {"ok": True, "status": "private_legacy"}
    if body.action == "delete":
        return {"ok": True, "status": "deleted",
                "deleted": await db.delete_public_review(user["id"], film_id)}
    item = await db.publish_legacy_review(user["id"], film_id)
    if item is None:
        raise HTTPException(
            status_code=409,
            detail="Чтобы опубликовать старую заметку, сначала поставьте оценку",
        )
    return {"ok": True, "status": "published", "item": item}


@app.post("/api/movie/{film_id}/reviews/{review_id}/report")
async def report_movie_review(film_id: int, review_id: int, body: ReviewReportBody,
                              user: dict = Depends(current_user)):
    _guard_review_write(user["id"])
    created = await db.report_review(
        user["id"], film_id, review_id, (body.reason or "").strip() or None,
    )
    if not created:
        raise HTTPException(
            status_code=409,
            detail="Отзыв недоступен, принадлежит вам или жалоба уже отправлена",
        )
    return {"ok": True}


@app.patch("/api/admin/reviews/{review_id}/hide")
async def admin_hide_review(review_id: int, user: dict = Depends(require_editor)):
    hidden = await db.hide_review(review_id)
    if not hidden:
        raise HTTPException(status_code=404, detail="Опубликованный отзыв не найден")
    role = await _effective_role(user["id"]) or "editor"
    await db.write_audit(
        user["id"], role, "review.hidden", "review", review_id,
        {"film_id": hidden["film_id"], "author_id": hidden["user_id"]},
    )
    return {"ok": True}


@app.delete("/api/movie/{film_id}")
async def delete(film_id: int, user: dict = Depends(throttled_mutation)):
    await db.remove_from_list(user["id"], film_id)  # из своего списка; в каталоге остаётся
    await _invalidate_stats_for(user["id"])
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
    if imdb_id:
        item = await db.get_catalog_item_by_source("i", imdb_id)
        if item:
            return {"items": [item]}
        # A direct ID missed the catalog, so only this first lookup reaches OMDb/KP.
        if not ratelimit.allow_user(user["id"]):
            raise HTTPException(status_code=429, detail="Слишком много запросов, подождите минуту")
        await db_runtime.release_request_connection_if_idle()
        d = await search.fetch_details("i", imdb_id)
        if not d:
            return {"items": []}
        # A direct IMDb lookup is still a first encounter with a film: keep it
        # in the permanent catalog immediately, not only after the user taps Add.
        await db.get_or_create_film(**d)
        item = await db.get_catalog_item_by_source("i", d["imdb_id"])
        return {"items": [item] if item else []}
    # Throttle считается внутри — только если реально идём в API (кэш-хиты бесплатны).
    res = await search.cached_search(q, user["id"])
    if res["throttled"]:
        raise HTTPException(status_code=429, detail="Слишком много запросов, подождите минуту")
    return {"items": res["items"], "limited": res["limited"]}


async def _resolve_film_id(src: str, ref: str, *, user_id: int | None = None) -> int:
    """Дедуп до внешних API + fetch_details + get_or_create_film — общий путь для
    /api/add и /api/admin/collections/{id}/films. Для src="i" ref == imdb_id, и если
    фильм уже в общем каталоге — линкуем сразу, не тратя лимит kinopoisk/OMDb."""
    ref = ref.strip()
    if src == "id":
        # Фильм уже в каталоге (редактор подборки шлёт внутренний ID) — внешние
        # провайдеры и их лимиты здесь не нужны.
        if not re.fullmatch(r"\d{1,12}", ref):
            raise HTTPException(status_code=422, detail="Некорректный идентификатор фильма")
        if not await db.get_film(int(ref)):
            raise HTTPException(status_code=404, detail="Фильм не найден")
        return int(ref)
    if src == "k" and not re.fullmatch(r"\d{1,12}", ref):
        raise HTTPException(status_code=422, detail="Некорректный идентификатор фильма")
    if src == "i" and not re.fullmatch(r"tt\d{5,12}", ref):
        raise HTTPException(status_code=422, detail="Некорректный IMDb идентификатор")

    film_id = await db.get_film_id_by_source(src, ref)
    if film_id is None:
        # A direct /api/add can otherwise be used as an unthrottled movie-ID
        # scanner.  Existing catalogue films stay instant and free; only the
        # first external lookup consumes the same per-user allowance as search.
        if user_id is not None and not ratelimit.allow_user(user_id):
            raise HTTPException(status_code=429, detail="Слишком много запросов, подождите минуту")
        await db_runtime.release_request_connection_if_idle()
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
async def add(body: AddBody, user: dict = Depends(throttled_mutation)):
    if body.src not in ("k", "i"):
        raise HTTPException(status_code=422, detail="Неизвестный источник")
    if body.status not in ("want_to_watch", "watched"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    film_id = await _resolve_film_id(body.src, body.ref, user_id=user["id"])
    watched_at = datetime.now(UTC).isoformat() if body.status == "watched" else None
    added = await db.add_to_list(user["id"], film_id, body.status, watched_at)
    await db.sync_film_to_partner(user["id"], film_id)  # пара: партнёру фильм в «Хочу»
    if added:
        await _invalidate_stats_for(user["id"])
    if not added:
        return {"ok": False, "reason": "exists", "movie_id": film_id}
    return {"ok": True, "movie_id": film_id}


async def _invalidate_stats_for(user_id: int) -> None:
    """Точечный сброс статистики: только затронутый пользователь и его пара.

    Раньше любая оценка вызывала stats_cache.clear() и вымывала кэш ВСЕХ
    пользователей — на публичном приложении это превращает кэш в бесполезный.
    Партнёра сбрасываем тоже: общая статистика пары считается из обоих списков.
    """
    affected = [user_id]
    try:
        partner_id = await db.get_partner(user_id)
    except Exception:
        partner_id = None
    if partner_id:
        affected.append(partner_id)
    stats_cache.invalidate_users(affected)


# ── API: статистика (личная) и случайный фильм ────────────────────────────────
@app.get("/api/stats")
async def stats(user: dict = Depends(current_user)):
    year = datetime.now(UTC).year
    key = ("personal", user["id"], year)
    _schedule_profile_director_enrichment(user["id"])
    cached = stats_cache.get(key)
    if cached is not None:
        return cached
    s, year_stats = await asyncio.gather(
        db.get_user_stats(user["id"]), db.get_year_stats(user["id"], year)
    )
    s["year"] = year_stats
    return stats_cache.put(key, s)


@app.get("/api/stats/person")
async def stats_person_films(role: str = "actor", name: str = "", scope: str = "me",
                             user: dict = Depends(current_user)):
    """Exact watched-film drill-down for an actor/director profile card."""
    person_name = name.strip()
    if role not in {"actor", "director"}:
        raise HTTPException(status_code=422, detail="Неизвестная роль")
    if scope not in {"me", "pair"}:
        raise HTTPException(status_code=422, detail="Неизвестный режим")
    if not person_name or len(person_name) > 160:
        raise HTTPException(status_code=422, detail="Некорректное имя")
    if scope == "pair":
        pair = await db.get_pair(user["id"])
        if pair is None:
            raise HTTPException(status_code=409, detail="Пара не подключена")
        items = await db.get_pair_person_watched_films(
            user["id"], pair["partner_id"], pair["since"], role, person_name,
        )
    else:
        items = await db.get_person_watched_films(user["id"], role, person_name)
    return {"name": person_name, "role": role, "scope": scope, "items": items, "total": len(items)}


async def _enrich_profile_people_photos(user_id: int) -> None:
    """Refresh genuinely missing cast/director portraits off the profile path.

    The profile always opens from local data. This bounded task uses only
    Wikidata/Commons and retries an empty portrait no more than once in 14
    days, so a temporary upstream miss does not become permanent.
    """
    try:
        actor_films, director_films = await asyncio.gather(
            db.watched_films_needing_actor_photo_enrichment(user_id, _PROFILE_PORTRAIT_FILM_LIMIT),
            db.watched_films_needing_director_photo_enrichment(user_id, _PROFILE_PORTRAIT_FILM_LIMIT),
        )
        if not actor_films and not director_films:
            return

        # SPARQL accepts a batch of IMDb IDs, but sending a user's entire
        # history in one request makes a single slow Wikimedia response erase
        # the practical benefit.  Small serial batches plus a global gate keep
        # this off the UI path and prevent many simultaneous profile opens from
        # putting pressure on either our machines or Wikimedia.
        async with _profile_portrait_lookup_gate:
            for start in range(0, max(len(actor_films), len(director_films)), _PROFILE_PORTRAIT_BATCH_SIZE):
                actor_chunk = actor_films[start:start + _PROFILE_PORTRAIT_BATCH_SIZE]
                director_chunk = director_films[start:start + _PROFILE_PORTRAIT_BATCH_SIZE]
                actor_map, director_map = await asyncio.gather(
                    wikidata.get_cast_by_imdb([film["imdb_id"] for film in actor_chunk]),
                    wikidata.get_directors_by_imdb([film["imdb_id"] for film in director_chunk]),
                )
                for film in actor_chunk:
                    cast = actor_map.get(film["imdb_id"], [])
                    if cast:
                        await db.set_film_cast_from_wikidata(
                            film["id"], ", ".join(person["name"] for person in cast),
                            json.dumps(cast, ensure_ascii=False),
                        )
                    else:
                        await db.mark_film_actor_photos_checked(film["id"])
                for film in director_chunk:
                    directors = director_map.get(film["imdb_id"], [])
                    if directors:
                        await db.set_film_directors_from_wikidata(
                            film["id"], ", ".join(person["name"] for person in directors),
                            json.dumps(directors, ensure_ascii=False),
                        )
                    else:
                        await db.mark_film_director_photos_checked(film["id"])
        await _invalidate_stats_for(user_id)
    except Exception:
        logger.warning("Profile people enrichment failed for user %s", user_id, exc_info=True)
    finally:
        _director_profile_enrichment_users.discard(user_id)


def _schedule_profile_director_enrichment(user_id: int) -> None:
    if user_id in _director_profile_enrichment_users:
        return
    _director_profile_enrichment_users.add(user_id)
    task = db_runtime.create_background_task(_enrich_profile_people_photos(user_id), name=f"profile-people-{user_id}")
    _director_profile_enrichment_tasks.add(task)
    task.add_done_callback(_director_profile_enrichment_tasks.discard)


@app.post("/api/wishlist/random")
async def wishlist_random(user: dict = Depends(throttled_quiz)):
    """Рулетка по СВОЕМУ списку «Хочу посмотреть».

    POST, а не GET: выбор меняет состояние круга (фильм помечается показанным),
    и кэшировать такой ответ нельзя.
    """
    item = await db.pick_random_wishlist_film(user["id"])
    if item is None:
        raise HTTPException(status_code=404, detail="Список «Хочу посмотреть» пуст")
    # Режим изображения выбирает СЕРВЕР. Фронтенд не должен гадать по
    # backdrop_url, годится тот кадр или нет: ровно эта догадка и давала
    # растянутый вертикальный постер на всю ширину экрана.
    prepared = await db.prepare_random_wishlist_film(user["id"])
    return {
        "item": hero_media.public_media_row(item),
        "cycle": await db.wishlist_roulette_state(user["id"]),
        "next": _wishlist_prepared_envelope(user["id"], prepared),
    }


_WISHLIST_PREPARE_TOKEN_VERSION = 1
_WISHLIST_PREPARE_TOKEN_TTL = timedelta(minutes=5)


class WishlistPreparedConsumeBody(BaseModel):
    token: str = Field(min_length=32, max_length=1024)


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _wishlist_prepare_signing_key() -> bytes:
    # A dedicated key lets CI and non-Telegram environments exercise the real
    # signing path. Production safely falls back to the stable BOT_TOKEN.
    # Domain separation prevents these tokens from being useful elsewhere.
    secret = WISHLIST_PREPARE_SECRET or BOT_TOKEN or ADMIN_TOKEN
    if not secret:
        raise HTTPException(status_code=503, detail="Підготовка фільму тимчасово недоступна")
    return hashlib.sha256(f"wishlist-prepare-v1:{secret}".encode()).digest()


def _sign_wishlist_prepared(user_id: int, film_id: int, cycle_number: int) -> str:
    payload = {
        "v": _WISHLIST_PREPARE_TOKEN_VERSION,
        "u": int(user_id),
        "f": int(film_id),
        "c": int(cycle_number),
        "e": int((datetime.now(UTC) + _WISHLIST_PREPARE_TOKEN_TTL).timestamp()),
    }
    encoded = _urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(
        _wishlist_prepare_signing_key(), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_urlsafe_b64encode(signature)}"


def _verify_wishlist_prepared(token: str, user_id: int) -> dict:
    try:
        encoded, encoded_signature = token.split(".", 1)
        expected = hmac.new(
            _wishlist_prepare_signing_key(), encoded.encode("ascii"), hashlib.sha256).digest()
        supplied = _urlsafe_b64decode(encoded_signature)
        if (
            not hmac.compare_digest(
                encoded_signature,
                _urlsafe_b64encode(supplied),
            )
            or not hmac.compare_digest(expected, supplied)
        ):
            raise ValueError("signature")
        payload = json.loads(_urlsafe_b64decode(encoded))
        values = {key: int(payload[key]) for key in ("v", "u", "f", "c", "e")}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Недійсний токен підготовленого фільму") from None
    if values["v"] != _WISHLIST_PREPARE_TOKEN_VERSION:
        raise HTTPException(status_code=400, detail="Непідтримувана версія токена")
    if values["u"] != int(user_id):
        raise HTTPException(status_code=403, detail="Цей токен належить іншому користувачу")
    if values["e"] < int(datetime.now(UTC).timestamp()):
        raise HTTPException(status_code=410, detail="Підготовлений фільм застарів")
    if values["f"] <= 0 or values["c"] <= 0:
        raise HTTPException(status_code=400, detail="Недійсний токен підготовленого фільму")
    return values


def _wishlist_prepared_envelope(user_id: int, prepared: dict | None) -> dict | None:
    if not prepared:
        return None
    item, cycle_number = prepared["item"], int(prepared["cycle_number"])
    return {
        "item": hero_media.public_media_row(item),
        "token": _sign_wishlist_prepared(user_id, item["id"], cycle_number),
        "expires_in": int(_WISHLIST_PREPARE_TOKEN_TTL.total_seconds()),
    }


@app.post("/api/wishlist/random/prepare")
async def wishlist_random_prepare(user: dict = Depends(throttled_quiz)):
    """Read-only prefetch: select a card without recording a show."""
    prepared = await db.prepare_random_wishlist_film(user["id"])
    if prepared is None:
        raise HTTPException(status_code=404, detail="Список «Хочу посмотреть» пуст")
    return _wishlist_prepared_envelope(user["id"], prepared)


@app.post("/api/wishlist/random/consume")
async def wishlist_random_consume(
        body: WishlistPreparedConsumeBody, user: dict = Depends(throttled_quiz)):
    """Validate a prepared card and atomically record the real show."""
    payload = _verify_wishlist_prepared(body.token, user["id"])
    item = await db.consume_prepared_wishlist_film(
        user["id"], payload["f"], payload["c"])
    if item is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "WISHLIST_PICK_STALE",
                    "message": "Список або цикл змінився; підготуйте інший фільм"})
    prepared = await db.prepare_random_wishlist_film(user["id"])
    return {
        "item": hero_media.public_media_row(item),
        "cycle": await db.wishlist_roulette_state(user["id"]),
        "next": _wishlist_prepared_envelope(user["id"], prepared),
    }


@app.get("/api/random")
async def random_movie(user: dict = Depends(current_user)):
    """Прежний эндпоинт: оставлен для уже установленных клиентов."""
    item = await db.pick_random_wishlist_film(user["id"])
    return {"item": hero_media.public_media_row(item) if item else None}


# ── Adaptive recommendations ────────────────────────────────────────────────
class RecommendationStartBody(BaseModel):
    language: str = Field(default="ru", max_length=8)


class RecommendationAnswerBody(RecommendationStartBody):
    question_id: str = Field(min_length=1, max_length=64)
    answer_id: str = Field(min_length=1, max_length=64)


class RandomRecommendationBody(RecommendationStartBody):
    context: str = Field(default="solo", max_length=16)


class RecommendationReplaceBody(RecommendationStartBody):
    film_id: int
    # Роль сервер определяет сам по сохранённой подборке. Поле оставлено
    # необязательным ради уже установленных клиентов и только сверяется.
    role: str | None = Field(default=None, max_length=24)


class RecommendationFeedbackBody(BaseModel):
    action: str = Field(min_length=1, max_length=24)
    mode: str = Field(default="random", max_length=16)
    session_id: str | None = Field(default=None, max_length=96)
    # role и score сохранены в контракте ради установленных клиентов, но сервер
    # их ИГНОРИРУЕТ и берёт свои: иначе в аналитику подбора попадает всё, что
    # угодно прислать фронтенду.
    role: str | None = Field(default=None, max_length=24)
    score: float | None = None


def _recommendation_language(value: str) -> str:
    return "en" if value == "en" else "ru"


async def _active_partner_for_recommendation(user_id: int, context: str) -> int | None:
    if context != "pair":
        return None
    pair = await db.get_pair(user_id)
    if not pair:
        raise HTTPException(status_code=409, detail="Пара сейчас не подключена")
    return int(pair["partner_id"])


async def _admin_quiz_engine_meta(session: dict, user_id: int) -> dict:
    """Информация о движке — только проверенному на СЕРВЕРЕ редактору/админу.

    Это операционные данные обкатки, а не часть семантики подбора. Обычному
    пользователю знать внутреннюю версию движка незачем, и он не должен
    получать признак предпросмотра, который можно подделать или принять за
    возможность клиента.
    """
    role = await _effective_role(user_id)
    if role not in ("editor", "admin"):
        return {}
    engine = _require_supported_engine(session)
    return {"engine_version": engine.engine_version,
            "engine_preview": engine.engine_version == engines.ENGINE_V2}


async def _quiz_payload(session: dict, language: str, user_id: int) -> dict:
    current = session.get("current_question")
    partner_available = bool(await db.get_pair(user_id))
    question = public_question(current, language) if current else None
    # Pair mode is not a fake option: it is absent until there is an active
    # relationship. The server independently validates it on answer/results.
    if question and question["id"] == "c4" and not partner_available:
        question["options"] = [option for option in question["options"] if option["id"] != "pair"]
    engine_meta = await _admin_quiz_engine_meta(session, user_id)
    return {"id": session["id"], "version": session["version"], "state": session["state"],
            "question": question, "progress": len(session.get("answers") or {}),
            "total": 8, "pair_available": partner_available, **engine_meta}


@app.post("/api/recommendations/random")
async def recommendation_random(body: RandomRecommendationBody, user: dict = Depends(throttled_quiz)):
    language = _recommendation_language(body.language)
    if body.context not in {"solo", "pair"}:
        raise HTTPException(status_code=422, detail="Неизвестный контекст просмотра")
    partner_id = await _active_partner_for_recommendation(user["id"], body.context)
    item = await recommendations.pick_and_record_smart_random(user["id"], language, partner_id)
    if not item:
        raise HTTPException(status_code=404, detail={
            "code": "NO_ELIGIBLE_FILMS",
            "message": "Пока недостаточно подходящих фильмов для подбора",
            "recoverable": True,
        })
    return {"item": item, "context": body.context}


async def _engine_for_new_session(user_id: int) -> engines.EngineSpec:
    """Версия НОВОЙ сессии. Единственное место, где смотрят на флаг.

    Признак администратора резолвится на сервере — подделать его с клиента
    нельзя.
    """
    is_admin = (await _effective_role(user_id)) in ("editor", "admin")
    return engines.resolve_engine_for_new_session(is_admin=is_admin)


def _current_engine_versions() -> dict:
    """Версии V1 — то, чем считается подбор для обычного пользователя."""
    return engines.V1.as_session_versions()


def _require_supported_engine(session: dict) -> engines.EngineSpec:
    """Движок сессии или явный отказ.

    Незнакомая непустая версия бывает при откате сборки назад: сессия новее
    самого кода. Считать её через V1 значило бы молча подменить семантику, от
    чего версионирование и защищает.
    """
    try:
        return engines.resolve_for_session(session)
    except engines.UnknownRecommendationEngine:
        raise HTTPException(status_code=409, detail={
            "code": "SESSION_ENGINE_UNSUPPORTED",
            "message": "Этот подбор собран другой версией — начните опрос заново"}) from None


def _session_versions_match(session: dict) -> bool:
    """Совпадает ли семантика сессии с текущей.

    Сверяемся не с «текущим флагом», а с реестром: V2-сессия остаётся V2, даже
    если V2 только что выключили. NULL — сессия старше версионирования, её
    доигрываем прежней логикой, а не объявляем сломанной.
    """
    current = engines.resolve_for_session(session).as_session_versions()
    stored = {
        "question_version": session.get("version"),
        "engine_version": session.get("engine_version"),
        "policy_version": session.get("policy_version"),
        "taxonomy_version": session.get("taxonomy_version"),
        "feature_version": session.get("feature_version"),
    }
    return all(value is None or value == current[key] for key, value in stored.items())


@app.post("/api/recommendations/quiz/start")
async def recommendation_quiz_start(body: RecommendationStartBody, user: dict = Depends(throttled_quiz)):
    language = _recommendation_language(body.language)
    engine = await _engine_for_new_session(user["id"])
    session = await db.create_recommendation_session(
        user["id"], engine.question_version, secrets.token_urlsafe(18), "q1",
        versions=engine.as_session_versions())
    return await _quiz_payload(session, language, user["id"])


@app.get("/api/recommendations/quiz/{session_id}")
async def recommendation_quiz_get(session_id: str, language: str = "ru", user: dict = Depends(current_user)):
    session = await db.get_recommendation_session(user["id"], session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Опрос не найден")
    if session["state"] == "expired":
        raise HTTPException(status_code=410, detail="Срок опроса истёк")
    return await _quiz_payload(session, _recommendation_language(language), user["id"])


@app.post("/api/recommendations/quiz/{session_id}/answer")
async def recommendation_quiz_answer(session_id: str, body: RecommendationAnswerBody,
                                     user: dict = Depends(throttled_quiz)):
    from recommendation_questions import option_for
    language = _recommendation_language(body.language)
    session = await db.get_recommendation_session(user["id"], session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Опрос не найден")
    if session["state"] != "active":
        raise HTTPException(status_code=409, detail="Этот опрос уже завершён")
    _require_supported_engine(session)
    expected = session.get("current_question")
    if body.question_id != expected:
        raise HTTPException(status_code=409, detail="Вопрос уже изменился — обновите экран")
    option = option_for(body.question_id, body.answer_id)
    if not option:
        raise HTTPException(status_code=422, detail="Некорректный вариант ответа")
    if body.question_id == "c4" and body.answer_id == "pair":
        await _active_partner_for_recommendation(user["id"], "pair")
    answers = dict(session.get("answers") or {})
    answers[body.question_id] = body.answer_id
    next_id = next_question_id(answers)
    updated = await db.update_recommendation_session(
        user["id"], session_id, answers, next_id, "complete" if next_id is None else "active")
    if not updated:
        raise HTTPException(status_code=404, detail="Опрос не найден")
    return await _quiz_payload(updated, language, user["id"])


@app.post("/api/recommendations/quiz/{session_id}/back")
async def recommendation_quiz_back(session_id: str, body: RecommendationStartBody, user: dict = Depends(throttled_quiz)):
    session = await db.get_recommendation_session(user["id"], session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Опрос не найден")
    answers = dict(session.get("answers") or {})
    if answers:
        answers.pop(next(reversed(answers)))
    next_id = next_question_id(answers)
    updated = await db.update_recommendation_session(user["id"], session_id, answers, next_id, "active")
    return await _quiz_payload(updated, _recommendation_language(body.language), user["id"])


@app.post("/api/recommendations/quiz/{session_id}/restart")
async def recommendation_quiz_restart(session_id: str, body: RecommendationStartBody, user: dict = Depends(throttled_quiz)):
    # Перезапуск — новый проход: берём версию, доступную СЕЙЧАС.
    engine = await _engine_for_new_session(user["id"])
    session = await db.restart_recommendation_session(user["id"], session_id, "q1",
                                                      versions=engine.as_session_versions())
    if not session:
        raise HTTPException(status_code=404, detail="Опрос не найден")
    return await _quiz_payload(session, _recommendation_language(body.language), user["id"])


@app.get("/api/recommendations/quiz/{session_id}/results")
async def recommendation_quiz_results(session_id: str, language: str = "ru", user: dict = Depends(current_user)):
    locale = _recommendation_language(language)
    session = await db.get_recommendation_session(user["id"], session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Опрос не найден")
    if session["state"] != "complete":
        raise HTTPException(status_code=409, detail="Сначала завершите опрос")
    engine = _require_supported_engine(session)
    answers = session.get("answers") or {}
    partner_id = await _active_partner_for_recommendation(user["id"], "pair") if answers.get("c4") == "pair" else None
    # Метаданные движка одинаковы на обоих путях: и для сохранённой подборки,
    # и для только что посчитанной. Иначе значок появлялся бы лишь при первом
    # открытии результатов.
    engine_meta = await _admin_quiz_engine_meta(session, user["id"])
    if session.get("results") is not None:
        return {"id": session_id, "items": session["results"],
                "context": "pair" if partner_id else "solo", **engine_meta}
    # Роль резолвится на сервере: подделать её с клиента нельзя.
    is_admin = (await _effective_role(user["id"])) in ("editor", "admin")
    items = await recommendations.quiz_results(user["id"], answers, locale, partner_id,
                                               is_admin=is_admin,
                                               engine_version=engine.engine_version)
    saved = await db.save_recommendation_session_results(user["id"], session_id, items)
    if not saved:
        raise HTTPException(status_code=404, detail="Опрос не найден")
    for item in items:
        await db.record_recommendation_history(user["id"], item["id"], "quiz", session_id=session_id,
                                               role=item["role"], score=item["score"])
    return {"id": session_id, "items": items,
            "context": "pair" if partner_id else "solo", **engine_meta}


@app.post("/api/recommendations/quiz/{session_id}/replace")
async def recommendation_quiz_replace(session_id: str, body: RecommendationReplaceBody,
                                      user: dict = Depends(throttled_quiz)):
    """Заменить один отклонённый вариант, не трогая остальные.

    Раньше «не предлагать» выбрасывало человека на стартовый экран подбора: он
    терял и результаты, и пройденную анкету. Теперь отклонённый фильм меняется
    на месте, а два оставшихся сохраняются как есть.
    """
    locale = _recommendation_language(body.language)
    session = await db.get_recommendation_session(user["id"], session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Опрос не найден")
    if session["state"] != "complete":
        raise HTTPException(status_code=409, detail="Сначала завершите опрос")
    current = list(session.get("results") or [])
    rejected = next((item for item in current if int(item.get("id") or 0) == body.film_id), None)
    if rejected is None:
        raise HTTPException(status_code=404, detail="Этот вариант не из текущей подборки")
    # Роль берётся ИЗ СОХРАНЁННОЙ подборки, а не из тела запроса. Клиент мог
    # прислать чужую роль, и она молча уходила и в карточку, и в историю
    # рекомендаций, портя аналитику. body.role остаётся в контракте ради
    # установленных клиентов, но расхождение — это ошибка, а не подсказка.
    server_role = str(rejected.get("role") or "")
    if body.role and body.role != server_role:
        raise HTTPException(status_code=422, detail="Роль не совпадает с текущей подборкой")
    engine = _require_supported_engine(session)
    if not _session_versions_match(session):
        # Подборка собрана прежней семантикой. Досчитывать её новой — молча
        # смешать два движка в одном экране; честнее попросить пройти заново.
        raise HTTPException(status_code=409, detail={
            "code": "SESSION_VERSION_MISMATCH",
            "message": "Подбор обновился — пройдите опрос заново"})
    answers = session.get("answers") or {}
    partner_id = await _active_partner_for_recommendation(user["id"], "pair") if answers.get("c4") == "pair" else None
    is_admin = (await _effective_role(user["id"])) in ("editor", "admin")
    kept_ids = {int(item["id"]) for item in current if int(item["id"]) != body.film_id}
    # Отклонённый и уже показанные исключены на уровне SQL (rejected + cooldown),
    # поэтому пересчёт не может вернуть их обратно.
    fresh = await recommendations.quiz_results(user["id"], answers, locale, partner_id,
                                               is_admin=is_admin,
                                               engine_version=engine.engine_version)
    candidates = [item for item in fresh if int(item["id"]) not in kept_ids]
    replacement = next((item for item in candidates if item.get("role") == server_role), None) \
        or (candidates[0] if candidates else None)
    if replacement is not None:
        replacement = {**replacement, "role": server_role}
    updated = [item for item in current if int(item["id"]) != body.film_id]
    if replacement is not None:
        # Позиция роли сохраняется: карточка меняется на месте, экран не прыгает.
        position = next((i for i, item in enumerate(current) if int(item["id"]) == body.film_id), len(updated))
        updated.insert(position, replacement)
    await db.save_recommendation_session_results(user["id"], session_id, updated)
    if replacement is not None:
        await db.record_recommendation_history(user["id"], replacement["id"], "quiz", session_id=session_id,
                                               role=server_role, score=replacement["score"])
    engine_meta = await _admin_quiz_engine_meta(session, user["id"])
    return {"id": session_id, "items": updated, "replacement": replacement,
            "context": "pair" if partner_id else "solo", **engine_meta}


@app.post("/api/recommendations/{film_id}/feedback")
async def recommendation_feedback(film_id: int, body: RecommendationFeedbackBody,
                                  user: dict = Depends(current_user)):
    if body.action not in {"opened", "want", "watched", "rejected", "another"}:
        raise HTTPException(status_code=422, detail="Некорректное действие")
    if body.mode not in {"random", "quiz", "wishlist"}:
        raise HTTPException(status_code=422, detail="Некорректный режим")
    if body.session_id and not await db.get_recommendation_session(user["id"], body.session_id):
        raise HTTPException(status_code=404, detail="Опрос не найден")
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    # Роль и счёт берём из СВОИХ данных, а не из тела запроса: клиент мог
    # прислать любые значения и засорить аналитику подбора. Для квиза источник
    # правды — сохранённые карточки сессии, для случайного режима — запись
    # показа в истории.
    metadata = await _server_recommendation_metadata(
        user["id"], film_id, body.mode, body.session_id)
    if metadata is None:
        # Не «запишем с пустыми полями», а отказ: строка истории без
        # подтверждённого происхождения — это мусор в аналитике подбора.
        raise HTTPException(status_code=409, detail="Фильм не относится к этой рекомендации")
    # В историю пишем режим из разрешённого схемой набора. «Хочу»-рулетка имеет
    # собственный учёт показов, но её отклонение — сигнал каталогу («не
    # предлагать»), поэтому строка ложится в тот же журнал под ролью wishlist.
    # Менять CHECK ради нового значения не нужно: роль различает источники.
    history_mode = "random" if body.mode == "wishlist" else body.mode
    await db.record_recommendation_history(user["id"], film_id, history_mode,
                                           session_id=body.session_id,
                                           role=metadata.role, score=metadata.score,
                                           action=body.action)
    return {"ok": True}


@dataclass(frozen=True)
class RecommendationMetadata:
    """Подтверждённое сервером происхождение показа."""
    role: str | None
    score: float | None


async def _server_recommendation_metadata(user_id: int, film_id: int, mode: str,
                                          session_id: str | None) -> RecommendationMetadata | None:
    """Метаданные показа ИЗ СВОИХ данных. None — сервер этот фильм в этом режиме
    не показывал, и записывать обратную связь не о чем.

    Между режимами не проваливаемся: если фильма нет в ЭТОЙ сессии квиза, брать
    показ из другой сессии нельзя — роль оттуда не имеет отношения к текущей
    карточке и портит аналитику ровно так же, как значение от клиента.
    """
    if mode == "quiz":
        if not session_id:
            return None
        session = await db.get_recommendation_session(user_id, session_id)
        if not session:
            return None
        item = next((item for item in session.get("results") or []
                     if int(item.get("id") or 0) == film_id), None)
        if item is None:
            return None
        score = item.get("score")
        return RecommendationMetadata(role=item.get("role"),
                                      score=float(score) if score is not None else None)
    if mode == "random":
        shown = await db.get_recommendation_shown(user_id, film_id, "random")
        if not shown:
            return None
        score = shown.get("score")
        return RecommendationMetadata(role=shown.get("role"),
                                      score=float(score) if score is not None else None)
    if mode == "wishlist":
        # Рулетка ведёт свой учёт показов и не пишет в историю рекомендаций.
        if not await db.was_wishlist_pick_shown(user_id, film_id):
            return None
        return RecommendationMetadata(role="wishlist", score=None)
    return None


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
# Публичные ручки отдают ТОЛЬКО published и не раскрывают редакционные поля
# (created_by/updated_at/version) — для этого отдельный сериализатор.
# display_type/backdrop нужны публичному рендеру (крупный блок vs обычная
# карточка), поэтому они в публичном наборе. Редакционные поля (version,
# created_by, updated_at, status) наружу по-прежнему не выходят.
_PUBLIC_COLLECTION_FIELDS = ("id", "title", "description", "cover", "backdrop",
                             "display_type", "film_count")


def _public_collection(row: dict) -> dict:
    return {k: row.get(k) for k in _PUBLIC_COLLECTION_FIELDS if k in row}


@app.get("/api/collections")
async def collections_list(user: dict = Depends(current_user)):
    """Публичные подборки. Крупные и обычные отдаются одним запросом и
    разделяются по display_type на клиенте — лишних round-trip'ов на главной нет."""
    return {"items": [_public_collection(c) for c in await db.list_collections(("published",))]}


@app.get("/api/collections/{collection_id}")
async def collection_detail(collection_id: int, user: dict = Depends(current_user)):
    c = await db.get_collection(collection_id, statuses=("published",))
    if not c:
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    public = _public_collection(c)
    public["items"] = await db.get_collection_films(collection_id, user["id"])
    return public


# ── API: администрирование подборок ───────────────────────────────────────────
# Каждая ручка независимо проверяет роль на сервере (require_editor). Тумблер
# «Режим администратора» во фронтенде — только UX и прав не даёт.
async def _audit(user: dict, action: str, entity_id, details: dict | None = None) -> None:
    role = await _effective_role(user["id"]) or "editor"
    await db.write_audit(user["id"], role, action, "collection", entity_id, details)


# Короткая память ключей идемпотентности создания. Держим в процессе намеренно:
# защита нужна от двойного тапа в пределах секунд, ради этого заводить таблицу и
# миграцию избыточно. Худший случай при рестарте/другом инстансе — поведение как
# раньше (возможен дубль), безопасности это не касается.
_CREATE_IDEMPOTENCY: dict[tuple[int, str], int] = {}
_CREATE_IDEMPOTENCY_LIMIT = 512


def _remember_create_key(user_id: int, key: str, collection_id: int) -> None:
    if len(_CREATE_IDEMPOTENCY) >= _CREATE_IDEMPOTENCY_LIMIT:
        for stale in list(_CREATE_IDEMPOTENCY)[:_CREATE_IDEMPOTENCY_LIMIT // 2]:
            _CREATE_IDEMPOTENCY.pop(stale, None)
    _CREATE_IDEMPOTENCY[(user_id, key)] = collection_id


def _safe_image_url(value: str) -> bool:
    """Только https и никаких javascript:/data:. Сервер по этим URL сам не ходит
    (никакого SSRF) — картинка грузится браузером через существующий прокси."""
    try:
        parsed = urlparse(str(value).strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def _conflict() -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "COLLECTION_VERSION_CONFLICT",
                                                  "message": "Подборку изменил другой администратор"})


class CollectionBody(BaseModel):
    title: str = Field(max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    # Редактор копит формат, фон и состав локально и присылает их первым
    # сохранением. Поля опциональные — старые клиенты остаются совместимыми.
    display_type: str | None = None
    cover_url: str | None = Field(default=None, max_length=2048)
    backdrop_url: str | None = Field(default=None, max_length=2048)
    ordered_film_ids: list[int] = Field(default_factory=list, max_length=200)


class CollectionPatchBody(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    cover_url: str | None = Field(default=None, max_length=2048)
    backdrop_url: str | None = Field(default=None, max_length=2048)
    display_type: str | None = None


class CollectionStatusBody(BaseModel):
    version: int = Field(ge=1)


class CollectionOrderBody(BaseModel):
    version: int = Field(ge=1)
    ordered_film_ids: list[int] = Field(min_length=1, max_length=200)


@app.get("/api/admin/collections", dependencies=[Depends(require_editor)])
async def admin_collections_list():
    return {"items": await db.list_collections(db.COLLECTION_STATUSES)}


@app.get("/api/admin/collections/{collection_id}", dependencies=[Depends(require_editor)])
async def admin_collection_detail(collection_id: int, user: dict = Depends(current_user)):
    c = await db.get_collection(collection_id)
    if not c:
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    c["items"] = await db.get_collection_films(collection_id, user["id"])
    return c


@app.post("/api/admin/collections", dependencies=[Depends(require_editor)])
async def collection_create(body: CollectionBody, user: dict = Depends(current_user),
                            idempotency_key: str = Header(default="", alias="Idempotency-Key")):
    title = " ".join(body.title.split())
    if not title:
        raise HTTPException(status_code=422, detail="Пустое название")
    display_type = body.display_type or "standard"
    if display_type not in db.COLLECTION_DISPLAY_TYPES:
        raise HTTPException(status_code=422, detail={
            "code": "INVALID_DISPLAY_TYPE", "message": "Неизвестный формат отображения"})
    for _url_field, value in (("cover_url", body.cover_url), ("backdrop_url", body.backdrop_url)):
        if value and not _safe_image_url(value):
            raise HTTPException(status_code=422, detail={
                "code": "IMAGE_URL_NOT_ALLOWED", "message": "Изображение: разрешён только https"})
    if len(set(body.ordered_film_ids)) != len(body.ordered_film_ids):
        raise HTTPException(status_code=422, detail={
            "code": "DUPLICATE_FILM_IDS", "message": "Дубли фильмов не допускаются"})
    # Повторный тап «Сохранить» с тем же ключом возвращает уже созданную
    # подборку вместо второй копии.
    if idempotency_key:
        cached = _CREATE_IDEMPOTENCY.get((user["id"], idempotency_key))
        if cached:
            return {"id": cached}
    collection_id = await db.create_collection(
        title[:80], user["id"], body.description, display_type=display_type,
        cover_url=body.cover_url, backdrop_url=body.backdrop_url,
        ordered_film_ids=body.ordered_film_ids)
    if collection_id is None:
        raise HTTPException(status_code=422, detail={
            "code": "UNKNOWN_FILM", "message": "Один из фильмов не найден"})
    if idempotency_key:
        _remember_create_key(user["id"], idempotency_key, collection_id)
    await _audit(user, "collection.created", collection_id, {
        "title": title[:80], "display_type": display_type,
        "film_ids": body.ordered_film_ids})
    return {"id": collection_id}


@app.patch("/api/admin/collections/{collection_id}", dependencies=[Depends(require_editor)])
async def collection_update(collection_id: int, body: CollectionPatchBody,
                            user: dict = Depends(current_user)):
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k != "version"}
    if "title" in fields:
        fields["title"] = " ".join(str(fields["title"]).split())[:80]
        if not fields["title"]:
            raise HTTPException(status_code=422, detail="Пустое название")
    for url_field in ("cover_url", "backdrop_url"):
        value = fields.get(url_field)
        if value and not _safe_image_url(value):
            raise HTTPException(status_code=422, detail={
                "code": "IMAGE_URL_NOT_ALLOWED", "message": "Изображение: разрешён только https"})
    if "display_type" in fields and fields["display_type"] not in db.COLLECTION_DISPLAY_TYPES:
        raise HTTPException(status_code=422, detail={
            "code": "INVALID_DISPLAY_TYPE", "message": "Неизвестный формат отображения"})
    existing = await db.get_collection(collection_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    # Крупный блок без картинки выглядит сломанным, поэтому опубликованную
    # подборку нельзя перевести в featured, если фон не из чего собрать.
    if (fields.get("display_type") == "featured" and existing["status"] == "published"
            and not (fields.get("backdrop_url") or existing.get("backdrop"))):
        raise HTTPException(status_code=422, detail={
            "code": "FEATURED_COLLECTION_IMAGE_REQUIRED",
            "message": "Для большой подборки нужно изображение"})
    updated = await db.update_collection(collection_id, body.version, user["id"], fields)
    if updated is None:
        raise _conflict()
    await _audit(user, "collection.updated", collection_id, {"fields": sorted(fields)})
    if "display_type" in fields and fields["display_type"] != existing.get("display_type"):
        await _audit(user, "collection.display_type_changed", collection_id,
                     {"before": existing.get("display_type"), "after": fields["display_type"]})
    if "backdrop_url" in fields and fields["backdrop_url"] != existing.get("backdrop_url"):
        await _audit(user, "collection.backdrop_changed", collection_id,
                     {"has_backdrop": bool(fields["backdrop_url"])})
    return updated


@app.delete("/api/admin/collections/{collection_id}", dependencies=[Depends(require_editor)])
async def collection_delete(collection_id: int, user: dict = Depends(current_user)):
    existing = await db.get_collection(collection_id)
    if existing and existing["status"] == "published":
        # Опубликованное сначала снимают с публикации/архивируют — так удаление
        # не может тихо выдернуть контент из-под пользователей.
        raise HTTPException(status_code=409, detail={
            "code": "COLLECTION_PUBLISHED", "message": "Сначала снимите с публикации"})
    await db.delete_collection(collection_id)
    await _audit(user, "collection.deleted", collection_id)
    return {"ok": True}


async def _transition(collection_id: int, new_status: str, version: int, user: dict, action: str):
    if not await db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    if new_status == "published":
        items = await db.get_collection_films(collection_id, user["id"])
        if not items:
            raise HTTPException(status_code=422, detail={
                "code": "COLLECTION_EMPTY", "message": "Нельзя опубликовать пустую подборку"})
        current = await db.get_collection(collection_id)
        # Для крупного блока картинка обязательна: пустой чёрный прямоугольник
        # на главной хуже, чем отсутствие блока.
        if current and current.get("display_type") == "featured" and not current.get("backdrop"):
            raise HTTPException(status_code=422, detail={
                "code": "FEATURED_COLLECTION_IMAGE_REQUIRED",
                "message": "Для большой подборки нужно изображение"})
    updated = await db.set_collection_status(collection_id, new_status, version, user["id"])
    if updated is None:
        raise _conflict()
    await _audit(user, action, collection_id, {"status": new_status})
    return updated


@app.post("/api/admin/collections/{collection_id}/publish", dependencies=[Depends(require_editor)])
async def collection_publish(collection_id: int, body: CollectionStatusBody,
                             user: dict = Depends(current_user)):
    return await _transition(collection_id, "published", body.version, user, "collection.published")


@app.post("/api/admin/collections/{collection_id}/unpublish", dependencies=[Depends(require_editor)])
async def collection_unpublish(collection_id: int, body: CollectionStatusBody,
                               user: dict = Depends(current_user)):
    return await _transition(collection_id, "draft", body.version, user, "collection.unpublished")


@app.post("/api/admin/collections/{collection_id}/archive", dependencies=[Depends(require_editor)])
async def collection_archive(collection_id: int, body: CollectionStatusBody,
                             user: dict = Depends(current_user)):
    return await _transition(collection_id, "archived", body.version, user, "collection.archived")


@app.post("/api/admin/collections/{collection_id}/restore", dependencies=[Depends(require_editor)])
async def collection_restore(collection_id: int, body: CollectionStatusBody,
                             user: dict = Depends(current_user)):
    return await _transition(collection_id, "draft", body.version, user, "collection.restored")


@app.put("/api/admin/collections/{collection_id}/items/order",
         dependencies=[Depends(require_editor)])
async def collection_reorder(collection_id: int, body: CollectionOrderBody,
                             user: dict = Depends(current_user)):
    if len(set(body.ordered_film_ids)) != len(body.ordered_film_ids):
        raise HTTPException(status_code=422, detail="Дубли в списке порядка")
    if not await db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    updated = await db.reorder_collection_items(
        collection_id, body.ordered_film_ids, body.version, user["id"])
    if updated is None:
        raise _conflict()
    await _audit(user, "collection.reordered", collection_id, {"count": len(body.ordered_film_ids)})
    return updated


class ResolveFilmBody(BaseModel):
    src: str
    ref: str = Field(max_length=128)


class HeroPresentationPatch(BaseModel):
    fit: Literal["contain", "cover"]
    focus_x: float = Field(default=0.5, ge=0.0, le=1.0)
    focus_y: float = Field(default=0.36, ge=0.0, le=1.0)


class PosterDisplayPatch(BaseModel):
    state: Literal["auto", "approved", "rejected"]
    reason: str | None = Field(default=None, max_length=200)


class MovieFlowPatch(BaseModel):
    state: Literal["auto", "allow", "exclude"]
    reason: str | None = Field(default=None, max_length=200)


@app.patch("/api/admin/films/{film_id}/hero-presentation",
           dependencies=[Depends(require_editor)])
async def admin_update_hero_presentation(
        film_id: int, body: HeroPresentationPatch,
        user: dict = Depends(current_user)):
    updated = await db.update_film_hero_presentation(
        film_id, fit=body.fit, focus_x=body.focus_x, focus_y=body.focus_y)
    if updated is None:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    return hero_media.hero_payload(updated)


@app.patch("/api/admin/films/{film_id}/poster-display",
           dependencies=[Depends(require_editor)])
async def admin_update_poster_display(
        film_id: int, body: PosterDisplayPatch,
        user: dict = Depends(current_user)):
    updated = await db.update_film_poster_display(
        film_id, state=body.state, reason=body.reason)
    if updated is None:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    return hero_media.hero_payload(updated)


@app.patch("/api/admin/films/{film_id}/movie-flow",
           dependencies=[Depends(require_editor)])
async def admin_update_movie_flow(
        film_id: int, body: MovieFlowPatch,
        user: dict = Depends(current_user)):
    updated = await db.update_film_movie_flow(
        film_id, state=body.state, reason=body.reason)
    if updated is None:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    await _audit(user, "film.movie_flow_updated", film_id, {
        "state": body.state, "reason": body.reason})
    return {
        "id": updated["id"],
        "movie_flow_state": updated.get("movie_flow_state") or "auto",
        "movie_flow_reason": updated.get("movie_flow_reason"),
    }


@app.post("/api/admin/films/resolve", dependencies=[Depends(require_editor)])
async def admin_resolve_film(body: ResolveFilmBody, user: dict = Depends(current_user)):
    """Затянуть фильм в общий каталог и вернуть его ID — без добавления в личный
    список редактора. Нужен редактору подборки: он копит состав локально, а в
    свои «Хочу»/«Смотрел» куратор фильмы при этом не набирает."""
    if body.src not in ("k", "i", "id"):
        raise HTTPException(status_code=422, detail="Неизвестный источник")
    film_id = await _resolve_film_id(body.src, body.ref, user_id=user["id"])
    film = await db.get_film(film_id)
    return {"id": film_id, "title": film.get("title") if film else None,
            "year": film.get("year") if film else None,
            "poster_url": film.get("poster_url") if film else None,
            "backdrop_url": film.get("backdrop_url") if film else None}


class FeaturedOrderBody(BaseModel):
    ordered_ids: list[int] = Field(min_length=1, max_length=50)


@app.put("/api/admin/collections/featured/order", dependencies=[Depends(require_editor)])
async def featured_reorder(body: FeaturedOrderBody, user: dict = Depends(current_user)):
    if len(set(body.ordered_ids)) != len(body.ordered_ids):
        raise HTTPException(status_code=422, detail="Дубли в списке порядка")
    if not await db.reorder_collections(body.ordered_ids, user["id"]):
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    await _audit(user, "collection.featured_reordered", ",".join(map(str, body.ordered_ids)),
                 {"count": len(body.ordered_ids)})
    return {"ok": True}


class CollectionAddBody(BaseModel):
    src: str
    ref: str = Field(max_length=128)


@app.post("/api/admin/collections/{collection_id}/films", dependencies=[Depends(require_editor)])
async def collection_add_film(collection_id: int, body: CollectionAddBody,
                              user: dict = Depends(current_user)):
    # "id" — фильм уже в каталоге (его шлёт редактор подборки); "k"/"i" — импорт извне.
    if body.src not in ("k", "i", "id"):
        raise HTTPException(status_code=422, detail="Неизвестный источник")
    if not await db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    film_id = await _resolve_film_id(body.src, body.ref)
    added = await db.add_film_to_collection(collection_id, film_id, user["id"])
    if added:
        await _audit(user, "collection.items_added", collection_id, {"film_id": film_id})
    return {"ok": True, "added": added, "movie_id": film_id}


@app.delete("/api/admin/collections/{collection_id}/films/{film_id}",
            dependencies=[Depends(require_editor)])
async def collection_remove_film(collection_id: int, film_id: int,
                                 user: dict = Depends(current_user)):
    await db.remove_film_from_collection(collection_id, film_id)
    await _audit(user, "collection.item_removed", collection_id, {"film_id": film_id})
    return {"ok": True}


@app.get("/api/admin/audit-log", dependencies=[Depends(require_editor)])
async def admin_audit_log(limit: int = 50):
    return {"items": await db.list_audit_log(max(1, min(200, limit)))}


@app.get("/api/admin/analytics", dependencies=[Depends(require_admin_user)])
async def admin_analytics():
    """Размер и происхождение аудитории. Только агрегаты.

    Ни одного идентификатора, имени или @username: чтобы понять, растёт ли
    продукт, знать, КТО именно зарегистрировался, не нужно, а выгрузка людей
    из админки — это уже совсем другой уровень доступа к чужим данным.
    """
    return await db.user_analytics()


# ── API: диагностика обогащения (только исключения) ───────────────────────────
# Владелец не должен просматривать каждый фильм. Сюда попадает только то, с чем
# автоматика не справилась: низкая уверенность, противоречия, мёртвые задания.
class ProfileOverrideBody(BaseModel):
    override: dict
    reason: str = Field(min_length=3, max_length=200)


@app.get("/api/admin/enrichment", dependencies=[Depends(require_editor)])
async def admin_enrichment_status():
    """Операционная сводка. Без единого идентификатора пользователя, запроса,
    токена и куска стека — диагностика не должна становиться утечкой."""
    from enrichment import queue as enrichment_queue
    from enrichment import repository as enrichment_repository
    from enrichment.semantic import build_classifier
    from enrichment.taxonomy import MOVIE_FEATURE_VERSION, MOVIE_TAXONOMY_VERSION, RULE_EXTRACTOR_VERSION
    return {
        "build": {
            "commit": os.getenv("FLY_MACHINE_VERSION") or os.getenv("GIT_COMMIT_SHA") or None,
            "region": os.getenv("FLY_REGION") or None,
        },
        "versions": {
            "feature": MOVIE_FEATURE_VERSION,
            "taxonomy": MOVIE_TAXONOMY_VERSION,
            "extractor": RULE_EXTRACTOR_VERSION,
            "semantic_classifier": getattr(build_classifier(), "model_version", None),
        },
        "flags": {
            "mood_layer_enabled": recommendations.MOOD_LAYER_ENABLED,
            "mood_layer_admin_preview": recommendations.MOOD_LAYER_ADMIN_PREVIEW,
            "smart_random_strategies": recommendations.SMART_RANDOM_STRATEGIES,
            "kinopoisk_hero_enabled": KINOPOISK_HERO_ENABLED,
            "fanart_hero_enabled": FANART_HERO_ENABLED,
            "fanart_configured": fanart.configured(),   # НАСТРОЕН ли ключ, а не какой
            "fullscreen_single_pick": FULLSCREEN_SINGLE_PICK_ENABLED,
        },
        "hero_media": await db.hero_distribution(),
        "engines": engines.engine_state(is_admin=True),
        "queue": await enrichment_queue.stats(),
        "oldest_pending_job": await enrichment_repository.oldest_pending_job(),
        "worker_heartbeat": await enrichment_repository.heartbeat(),
        "profiles": await enrichment_repository.distribution(),
    }


@app.get("/api/admin/enrichment/exceptions", dependencies=[Depends(require_editor)])
async def admin_enrichment_exceptions(limit: int = 50):
    """Очередь ручного разбора: только исключения, не весь каталог."""
    from enrichment import repository as enrichment_repository
    return {"items": await enrichment_repository.exceptions(max(1, min(200, limit)))}


@app.post("/api/admin/enrichment/{film_id}/rebuild")
async def admin_enrichment_rebuild(film_id: int, user: dict = Depends(require_editor)):
    from enrichment import service as enrichment_service
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    created = await enrichment_service.enqueue_for_film(film_id, priority=20)
    await db.write_audit(user["id"], "enrichment.rebuild", "film", film_id, {})
    return {"ok": True, "queued": created}


@app.put("/api/admin/enrichment/{film_id}/override")
async def admin_enrichment_override(film_id: int, body: ProfileOverrideBody,
                                    user: dict = Depends(require_editor)):
    """Ручная правка. Переживает любой автоматический пересчёт."""
    from enrichment import repository as enrichment_repository
    from enrichment import service as enrichment_service
    from enrichment.merge import OVERRIDE_ALLOWED_KEYS
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    unknown = set(body.override) - OVERRIDE_ALLOWED_KEYS
    if unknown:
        # Полезная нагрузка проверяется по белому списку: произвольный JSON от
        # клиента в профиль не попадает.
        raise HTTPException(status_code=422, detail=f"Недопустимые поля: {', '.join(sorted(unknown))}")
    await enrichment_repository.set_override(film_id, body.override, reason=body.reason,
                                             created_by=user["id"])
    await enrichment_service.enqueue_for_film(film_id, priority=20)
    await db.write_audit(user["id"], "enrichment.override", "film", film_id,
                         {"fields": sorted(body.override)})
    return {"ok": True}


@app.delete("/api/admin/enrichment/{film_id}/override")
async def admin_enrichment_override_delete(film_id: int, user: dict = Depends(require_editor)):
    from enrichment import repository as enrichment_repository
    from enrichment import service as enrichment_service
    removed = await enrichment_repository.delete_override(film_id)
    if removed:
        await enrichment_service.enqueue_for_film(film_id, priority=20)
        await db.write_audit(user["id"], "enrichment.override_removed", "film", film_id, {})
    return {"ok": True, "removed": removed}


# ── API: пара (партнёрство) ───────────────────────────────────────────────────
BOT_USERNAME = os.getenv("BOT_USERNAME", "addictfilmbot")


def _invite_link(token: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?startapp=inv_{token}&mode=fullscreen"


def _movie_link(film_id: int) -> str:
    """Диплинк на конкретный фильм (startapp) — для кнопки «Поделиться»."""
    return f"https://t.me/{BOT_USERNAME}?startapp=film_{film_id}&mode=fullscreen"


def _partner_brief(u: dict | None, viewer_id: int | None = None) -> dict:
    if not u:
        return {"id": None, "name": "", "username": None, "photo_url": None, "avatar_url": None}
    photo_url = u.get("photo_url")
    return {"id": u["id"], "name": u.get("first_name") or "", "username": u.get("username"),
            "photo_url": photo_url,
            "avatar_url": None if photo_url or viewer_id is None else _avatar_url(viewer_id, u["id"])}


_AVATAR_URL_TTL_SECONDS = max(60, min(int(os.getenv("AVATAR_URL_TTL_SECONDS", "300")), 3600))


def _avatar_signature(viewer_id: int, user_id: int, expires_at: int) -> str:
    """Scoped capability: valid only for this viewer, partner and short TTL."""
    payload = f"avatar-v2:{viewer_id}:{user_id}:{expires_at}".encode()
    return hmac.new(BOT_TOKEN.encode(), payload, hashlib.sha256).hexdigest()[:40]


def _avatar_url(viewer_id: int, user_id: int) -> str | None:
    """Short-lived, revocable URL for a current partner's Telegram avatar."""
    if not BOT_TOKEN:
        return None
    expires_at = int(time.time()) + _AVATAR_URL_TTL_SECONDS
    sig = _avatar_signature(viewer_id, user_id, expires_at)
    return f"/api/avatar/{user_id}?viewer={viewer_id}&exp={expires_at}&sig={sig}"


@app.get("/api/partner")
async def partner(user: dict = Depends(current_user)):
    pid = await db.get_partner(user["id"])
    if pid is not None:
        return {"status": "paired", "partner": _partner_brief(await db.get_user(pid), user["id"])}
    token = await db.get_pending_invite(user["id"])
    if token:
        return {"status": "invited", "link": _invite_link(token), "code": token}
    return {"status": "none"}


@app.post("/api/partner/invite")
async def partner_invite(user: dict = Depends(throttled_mutation)):
    if await db.get_partner(user["id"]) is not None:
        raise HTTPException(status_code=409, detail="Пара уже есть")
    invite = await db.create_invite(user["id"], with_state=True)
    if invite is None:  # pair could have been created after the pre-check above
        raise HTTPException(status_code=409, detail="Пара уже есть")
    token = invite["token"]
    return {"link": _invite_link(token), "code": token}


@app.get("/api/partner/invite/{token}")
async def partner_invite_preview(token: str, user: dict = Depends(current_user)):
    """Preview the sender tied to a valid invite, without changing its state."""
    token = token.strip()
    if token.startswith("inv_"):
        token = token[4:]
    sender = await db.get_invite_sender(token)
    if not sender:
        raise HTTPException(status_code=404, detail="Приглашение недействительно или уже использовано")
    # Opening the direct invite is itself the invitation UX.  Remember a known
    # recipient for cancellation/expiry, but never create a duplicate inbox or
    # bot notification beside that invite card.
    await db.register_invite_recipient(token, user["id"])
    return {"inviter": _partner_brief(sender, user["id"])}


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
    await _invalidate_stats_for(user["id"])
    await pair_notifications.dispatch_pair_event(event=res["event"])
    return {"ok": True, "partner": _partner_brief(await db.get_user(res["partner_id"]), user["id"])}


@app.post("/api/partner/decline")
async def partner_decline(body: AcceptBody, user: dict = Depends(current_user)):
    token = body.token.strip()
    if token.startswith("inv_"):
        token = token[4:]
    result = await db.decline_invite(token, user["id"])
    if result["ok"]:
        await pair_notifications.dispatch_pair_event(event=result["event"])
    # Canonical roles stay server-side; clients only need the outcome.
    return {"ok": result["ok"], "reason": result.get("reason")}


@app.post("/api/partner/unpair")
async def partner_unpair(user: dict = Depends(current_user)):
    result = await db.unpair(user["id"])
    await _invalidate_stats_for(user["id"])
    if result["kind"] == "ended" or result["kind"] == "cancelled":
        await pair_notifications.dispatch_pair_event(event=result["event"])
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
    s["partner"] = _partner_brief(await db.get_user(pair["partner_id"]), user["id"])
    return s


# ── Обслуживание (админ по ADMIN_TOKEN) ──────────────────────────────────────
def require_admin(x_admin_token: str = Header(default="")) -> None:
    """Гейт для служебных эндпоинтов. Без заданного ADMIN_TOKEN — выключены (404)."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")
    if not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Не авторизован")


@app.get("/api/admin/performance", dependencies=[Depends(require_admin)])
async def performance_snapshot():
    """Bounded in-process route timings for production triage (never public)."""
    return _performance_snapshot()


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


@app.post("/api/admin/backfill-actor-photos", dependencies=[Depends(require_admin)])
async def backfill_actor_photos(limit: int = 200):
    """Refresh top cast/portraits from Wikidata+Commons without Kinopoisk quota."""
    return await posters.backfill_actor_photos(limit=max(1, min(limit, 500)))


@app.post("/api/admin/backfill-director-photos", dependencies=[Depends(require_admin)])
async def backfill_director_photos(limit: int = 200):
    """Refresh director portraits from Wikidata+Commons without Kinopoisk quota."""
    return await posters.backfill_director_photos(limit=max(1, min(limit, 500)))


@app.post("/api/admin/enrich-film-people/{imdb_id}", dependencies=[Depends(require_admin)])
async def enrich_film_people(imdb_id: str):
    """Immediately refresh one catalogue film — useful when a user reports it."""
    if not re.fullmatch(r"tt\d{5,12}", imdb_id):
        raise HTTPException(status_code=422, detail="Некорректный IMDb ID")
    film_id = await db.get_film_id_by_source("i", imdb_id)
    if film_id is None:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    enriched = await _enrich_film_people(film_id, imdb_id)
    return {"film_id": film_id, "enriched": enriched}


# ── Прокси постеров (обходит блокировку CDN на стороне клиента) ───────────────
# Картинки грузятся через наш домен, а не напрямую с Amazon/Yandex — работает
# везде, где открывается само приложение. Без авторизации (тег <img> её не шлёт).
_ALLOWED_IMG_HOSTS = {
    "m.media-amazon.com", "images-na.ssl-images-amazon.com", "ia.media-imdb.com",
    "avatars.mds.yandex.net", "st.kp.yandex.net", "image.openmoviedb.com",
    # Kinopoisk returns some backdrop URLs through the official TMDB image CDN.
    # `imagetmdb.com` is a different host and did not cover these real URLs.
    "image.tmdb.org", "kinopoiskapiunofficial.tech",
    # Portraits from the free Wikidata/Commons fallback redirect through these
    # two exact Wikimedia hosts. Keep the list explicit for SSRF protection.
    "commons.wikimedia.org", "upload.wikimedia.org",
    # Единственный CDN Fanart.tv: горизонтальные кадры для экрана подбора.
    fanart.IMAGE_HOST,
}
_ALLOWED_IMG_TYPES = {"image/avif", "image/gif", "image/jpeg", "image/png", "image/webp"}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_REDIRECTS = 3
_IMG_FETCH_CONCURRENCY = max(1, min(int(os.getenv("IMG_FETCH_CONCURRENCY", "8")), 32))
_IMG_FETCH_WAIT_SECONDS = max(0.1, min(float(os.getenv("IMG_FETCH_WAIT_SECONDS", "1")), 10.0))
_img_session: aiohttp.ClientSession | None = None
_img_fetch_gate = asyncio.BoundedSemaphore(_IMG_FETCH_CONCURRENCY)
_img_trim_task: asyncio.Task | None = None


async def _img_sess() -> aiohttp.ClientSession:
    global _img_session
    if _img_session is None or _img_session.closed:
        _img_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
            connector=aiohttp.TCPConnector(limit=_IMG_FETCH_CONCURRENCY, limit_per_host=4),
            headers={"User-Agent": "Mozilla/5.0"})
    return _img_session


@app.get("/api/avatar/{user_id}", include_in_schema=False)
async def telegram_avatar(user_id: int, viewer: int = 0, exp: int = 0, sig: str = ""):
    """Проксі аватара партнера без передачі Telegram bot token у браузер.

    Telegram не додає photo_url партнера в initData поточного користувача, тому
    для старих профілів добираємо останнє фото через Bot API. Посилання підписане
    серверним токеном і не дозволяє довільно використовувати цей endpoint.
    """
    now = int(time.time())
    expected_sig = _avatar_signature(viewer, user_id, exp) if BOT_TOKEN and viewer and exp else ""
    if (user_id <= 0 or viewer <= 0 or exp < now or exp > now + _AVATAR_URL_TTL_SECONDS
            or not expected_sig or not hmac.compare_digest(sig, expected_sig)):
        raise HTTPException(status_code=404, detail="Аватар не знайдено")
    pair = await db.get_pair(viewer)
    if not pair or pair["partner_id"] != user_id:
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
                        headers={"Cache-Control": f"private, max-age={_AVATAR_URL_TTL_SECONDS}"})
    except HTTPException:
        raise
    except (TimeoutError, aiohttp.ClientError, ValueError, KeyError) as error:
        raise HTTPException(status_code=404, detail="Аватар не знайдено") from error


# Дисковый кэш картинок на томе /data (пустует после миграции БД на Postgres).
# Смысл: раздача с локального диска стабильнее и быстрее, чем каждый раз ходить
# на CDN Яндекса/Amazon — меньше шансов оборвать медленное мобильное соединение.
def _resolve_image_cache_dir() -> str:
    """Каталог кэша картинок — только если в него реально можно писать.

    Контейнер работает не от root, а том Fly монтируется root-овым. Молча
    ловить отказ на каждом запросе — худший вариант: кэш «есть», но не работает.
    Проверяем запись один раз на старте и честно выключаем при отказе.
    """
    configured = os.getenv("IMG_CACHE_DIR") or ("/data/imgcache" if os.path.isdir("/data") else "")
    if not configured:
        return ""
    try:
        os.makedirs(configured, exist_ok=True)
        probe = os.path.join(configured, ".write-probe")
        with open(probe, "wb") as handle:
            handle.write(b"1")
        os.remove(probe)
        return configured
    except OSError as error:
        logger.warning("Дисковый кэш картинок отключён: в %s нельзя писать (%s)",
                       configured, error)
        return ""


_IMG_CACHE_DIR = _resolve_image_cache_dir()
_IMG_CACHE_MAX_BYTES = max(0, int(os.getenv("IMG_CACHE_MAX_BYTES", str(256 * 1024 * 1024))))
_IMG_CACHE_MAX_FILES = max(0, int(os.getenv("IMG_CACHE_MAX_FILES", "4000")))
_IMG_CACHE_TRIM_INTERVAL_SECONDS = max(5.0, float(os.getenv("IMG_CACHE_TRIM_INTERVAL_SECONDS", "60")))
_img_last_trim_scheduled = 0.0


def _image_client_key(request: Request) -> str:
    """Use Fly's client address only when a Fly proxy is known to be in front."""
    if os.getenv("FLY_APP_NAME"):
        fly_client_ip = request.headers.get("fly-client-ip", "").strip()
        if fly_client_ip:
            return fly_client_ip
    return request.client.host if request.client else "unknown"


def _trim_image_cache_sync() -> None:
    """Best-effort LRU eviction; the image cache must have a hard ceiling."""
    if not _IMG_CACHE_DIR or (_IMG_CACHE_MAX_BYTES <= 0 and _IMG_CACHE_MAX_FILES <= 0):
        return
    entries: list[tuple[float, str, int]] = []
    total_bytes = 0
    try:
        for root, _dirs, filenames in os.walk(_IMG_CACHE_DIR):
            for name in filenames:
                # Another process may currently be writing an atomic tmp file.
                if name.endswith(".tmp"):
                    continue
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if not os.path.isfile(path):
                    continue
                entries.append((stat.st_mtime, path, stat.st_size))
                total_bytes += stat.st_size
    except OSError:
        return

    if ((_IMG_CACHE_MAX_BYTES <= 0 or total_bytes <= _IMG_CACHE_MAX_BYTES)
            and (_IMG_CACHE_MAX_FILES <= 0 or len(entries) <= _IMG_CACHE_MAX_FILES)):
        return

    files_left = len(entries)
    for _mtime, path, size in sorted(entries):
        if ((_IMG_CACHE_MAX_BYTES <= 0 or total_bytes <= _IMG_CACHE_MAX_BYTES)
                and (_IMG_CACHE_MAX_FILES <= 0 or files_left <= _IMG_CACHE_MAX_FILES)):
            break
        try:
            os.remove(path)
        except OSError:
            continue
        total_bytes -= size
        files_left -= 1


def _schedule_image_cache_trim() -> None:
    """Keep potentially slow filesystem traversal off the request event loop."""
    global _img_trim_task, _img_last_trim_scheduled
    if _img_trim_task is not None and not _img_trim_task.done():
        return
    now = time.monotonic()
    if now - _img_last_trim_scheduled < _IMG_CACHE_TRIM_INTERVAL_SECONDS:
        return
    _img_last_trim_scheduled = now
    try:
        _img_trim_task = db_runtime.create_background_task(
            asyncio.to_thread(_trim_image_cache_sync), name="image-cache-trim")
    except RuntimeError:
        # Isolated unit calls can invoke this helper with no running loop.
        pass


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


def _read_cached_image_sync(path: str) -> tuple[bytes | None, str | None]:
    """Disk cache IO belongs in a thread, not in the asyncio event loop."""
    try:
        if os.path.getsize(path) > _MAX_IMAGE_BYTES:
            os.remove(path)
            return None, None
        with open(path, "rb") as handle:
            data = handle.read()
        ctype = _img_ctype(data)
        if not ctype:
            os.remove(path)
            return None, None
        os.utime(path, None)
        return data, ctype
    except OSError:
        return None, None


def _write_cached_image_sync(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


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


async def _open_remote_image_stream(url: str) -> tuple[aiohttp.ClientResponse, bytes, str]:
    """Open and validate a remote image before response headers are sent.

    Redirect targets are allowlisted one by one.  Only the small magic-byte
    prefix is read here; the rest stays in aiohttp's bounded stream buffer.
    """
    for attempt in (1, 2):
        response: aiohttp.ClientResponse | None = None
        try:
            current_url = url
            for _ in range(_MAX_IMAGE_REDIRECTS + 1):
                response = await (await _img_sess()).get(current_url, allow_redirects=False)
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    response.release()
                    response = None
                    if not location:
                        raise HTTPException(status_code=404, detail="Изображение не найдено")
                    current_url = urljoin(current_url, location)
                    if not _is_allowed_image_url(current_url):
                        raise HTTPException(status_code=400, detail="Недопустимый источник")
                    continue
                if response.status != 200:
                    response.release()
                    response = None
                    raise HTTPException(status_code=404, detail="Изображение не найдено")
                declared_ctype = (
                    response.headers.get("Content-Type", "")
                    .split(";", 1)[0].strip().lower()
                )
                if declared_ctype not in _ALLOWED_IMG_TYPES:
                    response.release()
                    response = None
                    raise HTTPException(
                        status_code=415, detail="Неподдерживаемый формат изображения")
                if (response.content_length is not None
                        and response.content_length > _MAX_IMAGE_BYTES):
                    response.release()
                    response = None
                    raise HTTPException(
                        status_code=413, detail="Изображение слишком большое")

                # Twelve bytes cover every supported signature (including
                # WEBP/AVIF).  Some test/upstream streams return fewer bytes in
                # one read, so keep filling the prefix until EOF or 12 bytes.
                prefix = bytearray()
                while len(prefix) < 12:
                    chunk = await response.content.read(12 - len(prefix))
                    if not chunk:
                        break
                    prefix.extend(chunk)
                detected_ctype = _img_ctype(bytes(prefix))
                if not detected_ctype:
                    response.release()
                    response = None
                    raise HTTPException(
                        status_code=415, detail="Неподдерживаемый формат изображения")
                return response, bytes(prefix), detected_ctype
            raise HTTPException(status_code=502, detail="Слишком много перенаправлений")
        except HTTPException:
            if response is not None:
                response.release()
            raise
        except (TimeoutError, aiohttp.ClientError) as error:
            if response is not None:
                response.close()
            if attempt == 2:
                raise HTTPException(
                    status_code=502, detail="Не удалось загрузить изображение") from error
    raise HTTPException(status_code=502, detail="Не удалось загрузить изображение")


async def _stream_image_body(content: aiohttp.StreamReader, prefix: bytes,
                             cache_path: str | None):
    """Yield a cold image while atomically filling its disk cache.

    If an upstream with no Content-Length crosses the hard limit, stop the
    stream and discard the partial cache file.  At that point HTTP headers are
    already sent, so a deliberately truncated (undecodable) body is safer than
    buffering an unbounded response or publishing a corrupt cache entry.
    """
    tmp_path: str | None = None
    cache_handle = None
    complete = False
    total = 0
    if cache_path:
        try:
            await asyncio.to_thread(os.makedirs, os.path.dirname(cache_path), exist_ok=True)
            tmp_path = (
                f"{cache_path}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
            )
            cache_handle = await asyncio.to_thread(open, tmp_path, "wb")
        except OSError:
            tmp_path = None
            cache_handle = None
    try:
        for chunk in (prefix,):
            total += len(chunk)
            yield chunk
            if cache_handle is not None:
                await asyncio.to_thread(cache_handle.write, chunk)
        async for chunk in content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > _MAX_IMAGE_BYTES:
                logger.warning("Cold image stream exceeded %s bytes; response truncated",
                               _MAX_IMAGE_BYTES)
                return
            yield chunk
            if cache_handle is not None:
                await asyncio.to_thread(cache_handle.write, chunk)
        complete = True
    except (TimeoutError, aiohttp.ClientError) as error:
        # Headers have already been sent.  End with an invalid/truncated image;
        # the browser's existing fallback can retry, while the cache remains
        # clean and the worker never accumulates the whole body in RAM.
        logger.warning("Cold image stream interrupted: %s", error)
    finally:
        if cache_handle is not None:
            await asyncio.to_thread(cache_handle.close)
        if tmp_path:
            try:
                if complete:
                    await asyncio.to_thread(os.replace, tmp_path, cache_path)
                    _schedule_image_cache_trim()
                else:
                    await asyncio.to_thread(os.remove, tmp_path)
            except OSError:
                pass


@app.get("/img")
async def img_proxy(request: Request, u: str):
    if len(u) > 4096:
        raise HTTPException(status_code=400, detail="Недопустимый источник")
    if not _is_allowed_image_url(u):
        raise HTTPException(status_code=400, detail="Недопустимый источник")  # анти-SSRF

    cache_path = _img_cache_path(u)
    if cache_path and os.path.exists(cache_path):
        cached_data, cached_ctype = await asyncio.to_thread(_read_cached_image_sync, cache_path)
        if cached_data is not None and cached_ctype is not None:
            return Response(content=cached_data, media_type=cached_ctype,
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    if not ratelimit.allow_image_proxy(_image_client_key(request)):
        raise HTTPException(
            status_code=429,
            detail="Забагато запитів до зображень, спробуйте за хвилину",
            headers={"Retry-After": str(ratelimit.IMAGE_PROXY_WINDOW)},
        )
    try:
        await asyncio.wait_for(_img_fetch_gate.acquire(), timeout=_IMG_FETCH_WAIT_SECONDS)
    except TimeoutError as error:
        raise HTTPException(status_code=503,
                            detail="Черга завантаження зображень переповнена") from error

    try:
        response, prefix, ctype = await _open_remote_image_stream(u)
    except Exception:
        _img_fetch_gate.release()
        raise

    async def body():
        try:
            async for chunk in _stream_image_body(response.content, prefix, cache_path):
                yield chunk
        finally:
            response.close()
            _img_fetch_gate.release()

    return StreamingResponse(
        body(),
        media_type=ctype,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            # Images are already compressed.  Explicit identity also prevents
            # GZipMiddleware from buffering the streaming response.
            "Content-Encoding": "identity",
        },
    )


# ── Фронтенд ─────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    # no-store: HTML всегда свежий, чтобы новые версии app.js/style.css (?v=) подхватывались.
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"),
                        headers={"Cache-Control": "no-store, max-age=0",
                                 "Content-Security-Policy": _HTML_CSP})

# Python 3.12 не знает webp в mimetypes → StaticFiles отдавал бы
# application/octet-stream. Браузеры картинку рендерят и так, но корректный
# Content-Type нужен для честного кэширования и строгих WebView.
mimetypes.add_type("image/webp", ".webp")


class VersionedStaticFiles(StaticFiles):
    """Serve JS and CSS so a deploy always reaches the Telegram WebView.

    Раньше app.js отдавался как immutable на год. Ставка была на то, что ?v= в
    index.html всегда меняют вместе с файлом, — и она не сыграла: app.js менялся,
    номер версии оставался прежним, и WebView законно продолжал показывать
    годовалую копию. Ошибка человека здесь стоит слишком дорого: пользователь
    остаётся на старом фронтенде, пока сам не почистит кэш.

    Поэтому оба файла теперь revalidate: браузер спрашивает сервер и получает
    304, если ничего не менялось. Это один условный запрос на запуск — цена,
    несопоставимая с зависшей версией интерфейса. Кэш картинок не затронут:
    у них URL меняется вместе с содержимым.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in ("app.js", "style.css") and response.status_code == 200:
            response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        return response


app.mount("/", VersionedStaticFiles(directory=FRONTEND_DIR), name="static")
