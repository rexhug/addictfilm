"""Media-facing API routes.

The signed Telegram avatar implementation remains in ``main`` while the
transport endpoint gets its own router.  The public image proxy deliberately
stays beside its streaming helpers until that service is split as one unit.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["media"])


async def _main_handler(name: str, *args, **kwargs):
    from main import __dict__ as handlers

    return await handlers[name](*args, **kwargs)


@router.get("/avatar/{user_id}")
async def telegram_avatar(user_id: int, viewer: int = 0, exp: int = 0, sig: str = ""):
    return await _main_handler("telegram_avatar", user_id, viewer, exp, sig)

