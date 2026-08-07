"""Statistics transport routes.

The calculation remains in ``main`` for this incremental extraction.  Keeping
the wrapper thin avoids two competing implementations while giving the API a
stable domain router.
"""

from deps import current_user
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/api", tags=["stats"])


async def _main_handler(name: str, *args, **kwargs):
    from main import __dict__ as handlers

    return await handlers[name](*args, **kwargs)


@router.get("/stats")
async def stats(user: dict = Depends(current_user)):
    return await _main_handler("stats", user)


@router.get("/stats/person")
async def stats_person_films(role: str = "actor", name: str = "", scope: str = "me",
                             user: dict = Depends(current_user)):
    return await _main_handler("stats_person_films", role, name, scope, user)
