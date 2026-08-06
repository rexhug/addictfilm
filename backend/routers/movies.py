"""Movie lists, ratings and public review transport routes."""
import asyncio
import logging
from datetime import UTC, datetime
from typing import Literal

import database as db
import db_runtime
import pair_activity_notifications
import ratelimit
import search
import stats_cache
from deps import current_user, throttled_mutation
from fastapi import APIRouter, Depends, HTTPException
from film_resolver import resolve_film_id
from pydantic import BaseModel, Field

router = APIRouter(tags=["movies"])
logger = logging.getLogger(__name__)


async def _invalidate_stats_for(user_id: int) -> None:
    affected = [user_id]
    try:
        partner_id = await db.get_partner(user_id)
    except Exception:
        partner_id = None
    if partner_id:
        affected.append(partner_id)
    stats_cache.invalidate_users(affected)


@router.get("/api/movies")
async def movies(status: str = "want_to_watch", sort: str = "date",
                 limit: int = 50, offset: int = 0, user: dict = Depends(current_user)):
    if status not in ("want_to_watch", "watched", "top"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    if sort not in ("date", "rating", "best", "worst", "new", "old"):
        raise HTTPException(status_code=422, detail="Неизвестная сортировка")
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    items, total = await asyncio.gather(
        db.get_user_films(user["id"], status, limit=limit, offset=offset, sort=sort),
        db.count_user_films(user["id"], status),
    )
    return {"items": items, "total": total}


class RateBody(BaseModel):
    rating: int = Field(ge=1, le=10)


@router.post("/api/movie/{film_id}/rate")
async def rate(film_id: int, body: RateBody, user: dict = Depends(throttled_mutation)):
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    result = await db.set_rating(user["id"], film_id, body.rating)
    await db.sync_film_to_partner(user["id"], film_id)
    await _invalidate_stats_for(user["id"])
    try:
        await pair_activity_notifications.notify_partner_film_rated(
            actor_id=user["id"], film_id=film_id, rating=body.rating,
            first_rating=result.first_rating,
        )
    except Exception:
        logger.warning("Partner rating notification failed: actor=%s film=%s",
                       user["id"], film_id, exc_info=True)
    return {"ok": True}


@router.delete("/api/movie/{film_id}/rate")
async def unrate(film_id: int, user: dict = Depends(current_user)):
    await db.clear_rating(user["id"], film_id)
    await _invalidate_stats_for(user["id"])
    return {"ok": True}


class StatusBody(BaseModel):
    status: str


@router.post("/api/movie/{film_id}/status")
async def set_status(film_id: int, body: StatusBody,
                     user: dict = Depends(throttled_mutation)):
    if body.status not in ("want_to_watch", "watched"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    if not await db.get_film(film_id):
        raise HTTPException(status_code=404, detail="Фильм не найден")
    await db.set_status(user["id"], film_id, body.status)
    await db.sync_film_to_partner(user["id"], film_id)
    await _invalidate_stats_for(user["id"])
    return {"ok": True}


class CommentBody(BaseModel):
    text: str = Field(max_length=500)


@router.post("/api/movie/{film_id}/comment")
async def comment(film_id: int, body: CommentBody, user: dict = Depends(current_user)):
    if not await db.get_film(film_id):
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


@router.get("/api/movie/{film_id}/reviews")
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


@router.put("/api/movie/{film_id}/review")
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


@router.delete("/api/movie/{film_id}/review")
async def delete_movie_review(film_id: int, user: dict = Depends(current_user)):
    _guard_review_write(user["id"])
    return {"ok": True, "deleted": await db.delete_public_review(user["id"], film_id)}


@router.post("/api/movie/{film_id}/review/legacy")
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
        raise HTTPException(status_code=409,
                            detail="Чтобы опубликовать старую заметку, сначала поставьте оценку")
    return {"ok": True, "status": "published", "item": item}


@router.post("/api/movie/{film_id}/reviews/{review_id}/report")
async def report_movie_review(film_id: int, review_id: int, body: ReviewReportBody,
                              user: dict = Depends(current_user)):
    _guard_review_write(user["id"])
    created = await db.report_review(user["id"], film_id, review_id,
                                     (body.reason or "").strip() or None)
    if not created:
        raise HTTPException(status_code=409,
                            detail="Отзыв недоступен, принадлежит вам или жалоба уже отправлена")
    return {"ok": True}


@router.delete("/api/movie/{film_id}")
async def delete(film_id: int, user: dict = Depends(throttled_mutation)):
    await db.remove_from_list(user["id"], film_id)
    await _invalidate_stats_for(user["id"])
    return {"ok": True}


@router.get("/api/search")
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
        if not ratelimit.allow_user(user["id"]):
            raise HTTPException(status_code=429, detail="Слишком много запросов, подождите минуту")
        await db_runtime.release_request_connection_if_idle()
        details = await search.fetch_details("i", imdb_id)
        if not details:
            return {"items": []}
        await db.get_or_create_film(**details)
        item = await db.get_catalog_item_by_source("i", details["imdb_id"])
        return {"items": [item] if item else []}
    res = await search.cached_search(q, user["id"])
    if res["throttled"]:
        raise HTTPException(status_code=429, detail="Слишком много запросов, подождите минуту")
    return {"items": res["items"], "limited": res["limited"]}


class AddBody(BaseModel):
    src: str
    ref: str = Field(max_length=128)
    status: str = "want_to_watch"


@router.post("/api/add")
async def add(body: AddBody, user: dict = Depends(throttled_mutation)):
    if body.src not in ("k", "i"):
        raise HTTPException(status_code=422, detail="Неизвестный источник")
    if body.status not in ("want_to_watch", "watched"):
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    film_id = await resolve_film_id(body.src, body.ref, user_id=user["id"])
    watched_at = datetime.now(UTC).isoformat() if body.status == "watched" else None
    added = await db.add_to_list(user["id"], film_id, body.status, watched_at)
    await db.sync_film_to_partner(user["id"], film_id)
    if added:
        await _invalidate_stats_for(user["id"])
    if not added:
        return {"ok": False, "reason": "exists", "movie_id": film_id}
    return {"ok": True, "movie_id": film_id}
