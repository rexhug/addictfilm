"""Every /api/admin endpoint, in one module.

31 of the application's routes were the admin surface, and they are the group
least entangled with the rest: they talk to the database, the film resolver and
the metrics window, and to nothing that only main owns. Keeping them here means
main no longer has to be read to answer "what can an editor do".

Nothing here imports main — that is the constraint the deps/film_resolver/
observability extractions exist to satisfy.
"""
import os
import re
from typing import Literal
from urllib.parse import urlparse

import database as db
import fanart
import hero_media
import posters
import recommendations
from config import (
    FANART_HERO_ENABLED,
    FULLSCREEN_SINGLE_PICK_ENABLED,
    KINOPOISK_HERO_ENABLED,
)
from deps import (
    _effective_role,
    current_user,
    require_admin,
    require_admin_user,
    require_editor,
)
from fastapi import APIRouter, Depends, Header, HTTPException
from film_resolver import resolve_film_id
from observability import performance_snapshot as performance_snapshot_value
from pydantic import BaseModel, Field
from recommendation import engines

router = APIRouter(prefix="/api/admin", tags=["admin"])


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


@router.get("/collections", dependencies=[Depends(require_editor)])
async def admin_collections_list():
    return {"items": await db.list_collections(db.COLLECTION_STATUSES)}


@router.get("/collections/{collection_id}", dependencies=[Depends(require_editor)])
async def admin_collection_detail(collection_id: int, user: dict = Depends(current_user)):
    c = await db.get_collection(collection_id)
    if not c:
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    c["items"] = await db.get_collection_films(collection_id, user["id"])
    return c


@router.post("/collections", dependencies=[Depends(require_editor)])
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


@router.patch("/collections/{collection_id}", dependencies=[Depends(require_editor)])
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


@router.delete("/collections/{collection_id}", dependencies=[Depends(require_editor)])
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


@router.post("/collections/{collection_id}/publish", dependencies=[Depends(require_editor)])
async def collection_publish(collection_id: int, body: CollectionStatusBody,
                             user: dict = Depends(current_user)):
    return await _transition(collection_id, "published", body.version, user, "collection.published")


@router.post("/collections/{collection_id}/unpublish", dependencies=[Depends(require_editor)])
async def collection_unpublish(collection_id: int, body: CollectionStatusBody,
                               user: dict = Depends(current_user)):
    return await _transition(collection_id, "draft", body.version, user, "collection.unpublished")


@router.post("/collections/{collection_id}/archive", dependencies=[Depends(require_editor)])
async def collection_archive(collection_id: int, body: CollectionStatusBody,
                             user: dict = Depends(current_user)):
    return await _transition(collection_id, "archived", body.version, user, "collection.archived")


@router.post("/collections/{collection_id}/restore", dependencies=[Depends(require_editor)])
async def collection_restore(collection_id: int, body: CollectionStatusBody,
                             user: dict = Depends(current_user)):
    return await _transition(collection_id, "draft", body.version, user, "collection.restored")


@router.put("/collections/{collection_id}/items/order",
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


@router.patch("/films/{film_id}/hero-presentation",
           dependencies=[Depends(require_editor)])
async def admin_update_hero_presentation(
        film_id: int, body: HeroPresentationPatch,
        user: dict = Depends(current_user)):
    updated = await db.update_film_hero_presentation(
        film_id, fit=body.fit, focus_x=body.focus_x, focus_y=body.focus_y)
    if updated is None:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    return hero_media.hero_payload(updated)


@router.patch("/films/{film_id}/poster-display",
           dependencies=[Depends(require_editor)])
async def admin_update_poster_display(
        film_id: int, body: PosterDisplayPatch,
        user: dict = Depends(current_user)):
    updated = await db.update_film_poster_display(
        film_id, state=body.state, reason=body.reason)
    if updated is None:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    return hero_media.hero_payload(updated)


@router.patch("/films/{film_id}/movie-flow",
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


@router.post("/films/resolve", dependencies=[Depends(require_editor)])
async def admin_resolve_film(body: ResolveFilmBody, user: dict = Depends(current_user)):
    """Затянуть фильм в общий каталог и вернуть его ID — без добавления в личный
    список редактора. Нужен редактору подборки: он копит состав локально, а в
    свои «Хочу»/«Смотрел» куратор фильмы при этом не набирает."""
    if body.src not in ("k", "i", "id"):
        raise HTTPException(status_code=422, detail="Неизвестный источник")
    film_id = await resolve_film_id(body.src, body.ref, user_id=user["id"])
    film = await db.get_film(film_id)
    return {"id": film_id, "title": film.get("title") if film else None,
            "year": film.get("year") if film else None,
            "poster_url": film.get("poster_url") if film else None,
            "backdrop_url": film.get("backdrop_url") if film else None}


class FeaturedOrderBody(BaseModel):
    ordered_ids: list[int] = Field(min_length=1, max_length=50)


@router.put("/collections/featured/order", dependencies=[Depends(require_editor)])
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


@router.post("/collections/{collection_id}/films", dependencies=[Depends(require_editor)])
async def collection_add_film(collection_id: int, body: CollectionAddBody,
                              user: dict = Depends(current_user)):
    # "id" — фильм уже в каталоге (его шлёт редактор подборки); "k"/"i" — импорт извне.
    if body.src not in ("k", "i", "id"):
        raise HTTPException(status_code=422, detail="Неизвестный источник")
    if not await db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    film_id = await resolve_film_id(body.src, body.ref)
    added = await db.add_film_to_collection(collection_id, film_id, user["id"])
    if added:
        await _audit(user, "collection.items_added", collection_id, {"film_id": film_id})
    return {"ok": True, "added": added, "movie_id": film_id}


@router.delete("/collections/{collection_id}/films/{film_id}",
            dependencies=[Depends(require_editor)])
async def collection_remove_film(collection_id: int, film_id: int,
                                 user: dict = Depends(current_user)):
    await db.remove_film_from_collection(collection_id, film_id)
    await _audit(user, "collection.item_removed", collection_id, {"film_id": film_id})
    return {"ok": True}


@router.get("/audit-log", dependencies=[Depends(require_editor)])
async def admin_audit_log(limit: int = 50):
    return {"items": await db.list_audit_log(max(1, min(200, limit)))}


@router.get("/analytics", dependencies=[Depends(require_admin_user)])
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


@router.get("/enrichment", dependencies=[Depends(require_editor)])
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


@router.get("/enrichment/exceptions", dependencies=[Depends(require_editor)])
async def admin_enrichment_exceptions(limit: int = 50):
    """Очередь ручного разбора: только исключения, не весь каталог."""
    from enrichment import repository as enrichment_repository
    return {"items": await enrichment_repository.exceptions(max(1, min(200, limit)))}


@router.post("/enrichment/{film_id}/rebuild")
async def admin_enrichment_rebuild(film_id: int, user: dict = Depends(require_editor)):
    from enrichment import service as enrichment_service
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    created = await enrichment_service.enqueue_for_film(film_id, priority=20)
    await db.write_audit(user["id"], "enrichment.rebuild", "film", film_id, {})
    return {"ok": True, "queued": created}


@router.put("/enrichment/{film_id}/override")
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


@router.delete("/enrichment/{film_id}/override")
async def admin_enrichment_override_delete(film_id: int, user: dict = Depends(require_editor)):
    from enrichment import repository as enrichment_repository
    from enrichment import service as enrichment_service
    removed = await enrichment_repository.delete_override(film_id)
    if removed:
        await enrichment_service.enqueue_for_film(film_id, priority=20)
        await db.write_audit(user["id"], "enrichment.override_removed", "film", film_id, {})
    return {"ok": True, "removed": removed}

# ── Обслуживание (админ по ADMIN_TOKEN) ──────────────────────────────────────
@router.get("/performance", dependencies=[Depends(require_admin)])
async def performanceperformance_snapshot_value():
    """Bounded in-process route timings for production triage (never public)."""
    return performance_snapshot_value()


@router.post("/backfill-posters", dependencies=[Depends(require_admin)])
async def backfill_posters(limit: int = 200, omdb_cap: int = 60):
    """Добрать постеры фильмам без картинки (kinopoisk → OMDb). Идемпотентно;
    вызывать повторно, пока remaining не станет 0."""
    return await posters.backfill(limit=max(1, min(limit, 500)), _omdb_cap=max(1, min(omdb_cap, 200)))


@router.post("/upgrade-omdb-posters", dependencies=[Depends(require_admin)])
async def upgrade_omdb_posters(limit: int = 200, name_cap: int = 60):
    """Заменить постеры Amazon/OMDb на kinopoisk-версии у уже добавленных фильмов.
    Идемпотентно; вызывать повторно, пока kept_omdb не перестанет уменьшаться."""
    return await posters.upgrade_omdb_posters(limit=max(1, min(limit, 500)), _name_cap=max(1, min(name_cap, 200)))


@router.post("/backfill-actor-photos", dependencies=[Depends(require_admin)])
async def backfill_actor_photos(limit: int = 200):
    """Refresh top cast/portraits from Wikidata+Commons without Kinopoisk quota."""
    return await posters.backfill_actor_photos(limit=max(1, min(limit, 500)))


@router.post("/backfill-director-photos", dependencies=[Depends(require_admin)])
async def backfill_director_photos(limit: int = 200):
    """Refresh director portraits from Wikidata+Commons without Kinopoisk quota."""
    return await posters.backfill_director_photos(limit=max(1, min(limit, 500)))


@router.post("/enrich-film-people/{imdb_id}", dependencies=[Depends(require_admin)])
async def enrich_film_people(imdb_id: str):
    """Immediately refresh one catalogue film — useful when a user reports it."""
    if not re.fullmatch(r"tt\d{5,12}", imdb_id):
        raise HTTPException(status_code=422, detail="Некорректный IMDb ID")
    film_id = await db.get_film_id_by_source("i", imdb_id)
    if film_id is None:
        raise HTTPException(status_code=404, detail="Фильм не найден")
    # Local import on purpose: the enrichment helper owns main's background-task
    # registry, so moving it here would split that state across two modules.
    # main imports this router, so a module-level import would be a real cycle;
    # by request time main is fully loaded and this one is not.
    from main import _enrich_film_people
    enriched = await _enrich_film_people(film_id, imdb_id)
    return {"film_id": film_id, "enriched": enriched}

@router.patch("/reviews/{review_id}/hide")
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
