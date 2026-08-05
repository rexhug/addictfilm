"""User notification API routes.

The router deliberately keeps the existing database contract and response
shapes.  It is a transport boundary only; notification creation and delivery
remain in the existing domain services.
"""
from typing import Literal

import database as db
from deps import current_user
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
async def notifications(
    limit: int = 20,
    before_id: int | None = None,
    category: Literal["all", "pair", "films", "system"] = "all",
    user: dict = Depends(current_user),
):
    return await db.list_notifications(
        user["id"], limit=limit, before_id=before_id, category=category
    )


@router.post("/read-all")
async def notifications_read_all(user: dict = Depends(current_user)):
    return {"updated": await db.mark_all_notifications_read(user["id"])}


@router.post("/{notification_id}/read")
async def notification_read(notification_id: int, user: dict = Depends(current_user)):
    return {"ok": await db.mark_notification_read(user["id"], notification_id)}
