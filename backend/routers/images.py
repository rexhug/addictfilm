"""Streaming image proxy transport route.

The validation, cache and streaming implementation stays in ``main`` for now;
this router only owns the public HTTP surface so it can be split safely.
"""

from fastapi import APIRouter, Request

router = APIRouter(tags=["media"])


async def _main_handler(name: str, *args, **kwargs):
    from main import __dict__ as handlers

    return await handlers[name](*args, **kwargs)


@router.get("/api/avatar/{user_id}")
async def telegram_avatar(user_id: int, viewer: int = 0, exp: int = 0, sig: str = ""):
    return await _main_handler("telegram_avatar", user_id, viewer, exp, sig)


@router.get("/img")
async def img_proxy(request: Request, u: str):
    return await _main_handler("img_proxy", request, u)
