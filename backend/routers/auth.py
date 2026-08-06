"""Bootstrap, profile and settings routes.

The handlers keep the public response contract stable while moving the HTTP
boundary out of ``main.py``.  Domain helpers remain in their existing modules.
"""
import database as db
from config import BOT_TOKEN, CAST_V2_ENABLED, FULLSCREEN_SINGLE_PICK_ENABLED
from deps import _effective_role, current_user
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["auth"])

_ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "editor": ("collections.read", "collections.write", "content.publish"),
    "admin": ("collections.read", "collections.write", "content.publish", "audit.read"),
}


def _client_features() -> dict:
    return {
        "fullscreen_single_pick": FULLSCREEN_SINGLE_PICK_ENABLED,
        "cast_v2": CAST_V2_ENABLED,
    }


@router.get("/api/me/capabilities")
async def me_capabilities(user: dict = Depends(current_user)):
    role = await _effective_role(user["id"])
    capabilities = _ROLE_CAPABILITIES.get(role or "", ())
    return {"is_admin": bool(capabilities), "admin_role": role if capabilities else None,
            "capabilities": list(capabilities)}


@router.get("/api/me")
async def me(user: dict = Depends(current_user)):
    settings = await db.get_notification_settings(user["id"])
    return {"id": user["id"], "label": user.get("first_name", ""),
            "username": user.get("username"), "photo_url": user.get("photo_url"),
            "role": await _effective_role(user["id"]), "telegram_available": bool(BOT_TOKEN),
            "features": _client_features(), **settings}


class SettingsBody(BaseModel):
    language: str | None = Field(default=None, max_length=8)
    telegram_notifications: bool | None = None


@router.get("/api/settings")
async def get_settings(user: dict = Depends(current_user)):
    return {**(await db.get_notification_settings(user["id"])),
            "telegram_available": bool(BOT_TOKEN)}


@router.patch("/api/settings")
async def patch_settings(body: SettingsBody, user: dict = Depends(current_user)):
    if body.language is not None and body.language not in ("ru", "en"):
        raise HTTPException(status_code=422, detail="Unsupported language")
    settings = await db.update_notification_settings(
        user["id"], language=body.language, telegram_enabled=body.telegram_notifications)
    return {**settings, "telegram_available": bool(BOT_TOKEN)}
