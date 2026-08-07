"""Public catalogue discovery and published collection routes."""
import database as db
from deps import current_user
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/api", tags=["browse"])
_PUBLIC_COLLECTION_FIELDS = ("id", "title", "description", "cover", "backdrop",
                             "display_type", "film_count")


def _public_collection(row: dict) -> dict:
    return {key: row.get(key) for key in _PUBLIC_COLLECTION_FIELDS if key in row}


@router.get("/browse")
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


@router.get("/genres")
async def genres(user: dict = Depends(current_user)):
    return {"items": await db.list_genres()}


@router.get("/collections")
async def collections_list(user: dict = Depends(current_user)):
    return {"items": [_public_collection(c) for c in await db.list_collections(("published",))]}


@router.get("/collections/{collection_id}")
async def collection_detail(collection_id: int, user: dict = Depends(current_user)):
    collection = await db.get_collection(collection_id, statuses=("published",))
    if not collection:
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    public = _public_collection(collection)
    public["items"] = await db.get_collection_films(collection_id, user["id"])
    return public
