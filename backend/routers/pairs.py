"""Pair lifecycle and pair-statistics API routes."""
import hashlib
import hmac
import os
import time

import database as db
import pair_notifications
import stats_cache
from config import BOT_TOKEN
from deps import current_user, throttled_mutation
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/partner", tags=["pairs"])
BOT_USERNAME = os.getenv("BOT_USERNAME", "addictfilmbot")
_AVATAR_URL_TTL_SECONDS = max(60, min(int(os.getenv("AVATAR_URL_TTL_SECONDS", "300")), 3600))


def _invite_link(token: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?startapp=inv_{token}&mode=fullscreen"


def _avatar_signature(viewer_id: int, user_id: int, expires_at: int) -> str:
    payload = f"avatar-v2:{viewer_id}:{user_id}:{expires_at}".encode()
    return hmac.new(BOT_TOKEN.encode(), payload, hashlib.sha256).hexdigest()[:40]


def _avatar_url(viewer_id: int, user_id: int) -> str | None:
    if not BOT_TOKEN:
        return None
    expires_at = int(time.time()) + _AVATAR_URL_TTL_SECONDS
    return f"/api/avatar/{user_id}?viewer={viewer_id}&exp={expires_at}&sig={_avatar_signature(viewer_id, user_id, expires_at)}"


def _partner_brief(user: dict | None, viewer_id: int | None = None) -> dict:
    if not user:
        return {"id": None, "name": "", "username": None, "photo_url": None, "avatar_url": None}
    photo_url = user.get("photo_url")
    return {"id": user["id"], "name": user.get("first_name") or "",
            "username": user.get("username"), "photo_url": photo_url,
            "avatar_url": None if photo_url or viewer_id is None else _avatar_url(viewer_id, user["id"])}


async def _invalidate_stats_for(user_id: int) -> None:
    affected = [user_id]
    try:
        partner_id = await db.get_partner(user_id)
    except Exception:
        partner_id = None
    if partner_id:
        affected.append(partner_id)
    stats_cache.invalidate_users(affected)


@router.get("")
async def partner(user: dict = Depends(current_user)):
    pid = await db.get_partner(user["id"])
    if pid is not None:
        return {"status": "paired", "partner": _partner_brief(await db.get_user(pid), user["id"])}
    token = await db.get_pending_invite(user["id"])
    if token:
        return {"status": "invited", "link": _invite_link(token), "code": token}
    return {"status": "none"}


@router.post("/invite")
async def partner_invite(user: dict = Depends(throttled_mutation)):
    if await db.get_partner(user["id"]) is not None:
        raise HTTPException(status_code=409, detail="Пара уже есть")
    invite = await db.create_invite(user["id"], with_state=True)
    if invite is None:
        raise HTTPException(status_code=409, detail="Пара уже есть")
    return {"link": _invite_link(invite["token"]), "code": invite["token"]}


@router.get("/invite/{token}")
async def partner_invite_preview(token: str, user: dict = Depends(current_user)):
    token = token.strip()
    if token.startswith("inv_"):
        token = token[4:]
    sender = await db.get_invite_sender(token)
    if not sender:
        raise HTTPException(status_code=404, detail="Приглашение недействительно или уже использовано")
    await db.register_invite_recipient(token, user["id"])
    return {"inviter": _partner_brief(sender, user["id"])}


class AcceptBody(BaseModel):
    token: str = Field(max_length=128)


@router.post("/accept")
async def partner_accept(body: AcceptBody, user: dict = Depends(current_user)):
    token = body.token.strip()
    if token.startswith("inv_"):
        token = token[4:]
    result = await db.accept_invite(token, user["id"])
    if not result["ok"]:
        return {"ok": False, "reason": result["reason"]}
    await _invalidate_stats_for(user["id"])
    await pair_notifications.dispatch_pair_event(event=result["event"])
    return {"ok": True, "partner": _partner_brief(await db.get_user(result["partner_id"]), user["id"])}


@router.post("/decline")
async def partner_decline(body: AcceptBody, user: dict = Depends(current_user)):
    token = body.token.strip()
    if token.startswith("inv_"):
        token = token[4:]
    result = await db.decline_invite(token, user["id"])
    if result["ok"]:
        await pair_notifications.dispatch_pair_event(event=result["event"])
    return {"ok": result["ok"], "reason": result.get("reason")}


@router.post("/unpair")
async def partner_unpair(user: dict = Depends(current_user)):
    result = await db.unpair(user["id"])
    await _invalidate_stats_for(user["id"])
    if result["kind"] in ("ended", "cancelled"):
        await pair_notifications.dispatch_pair_event(event=result["event"])
    return {"ok": True}


@router.get("/stats")
async def partner_stats(user: dict = Depends(current_user)):
    pair = await db.get_pair(user["id"])
    if pair is None:
        raise HTTPException(status_code=404, detail="Нет пары")
    key = ("pair", user["id"], pair["partner_id"], pair["since"])
    result = stats_cache.get(key)
    if result is None:
        result = await db.pair_period_stats(user["id"], pair["partner_id"], pair["since"])
        result = stats_cache.put(key, result)
    result["partner"] = _partner_brief(await db.get_user(pair["partner_id"]), user["id"])
    return result
