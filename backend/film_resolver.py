"""Turn an external movie reference into a catalogue film id.

Shared by /api/add and the collection editor: both must dedupe against the
catalogue before spending the kinopoisk/OMDb allowance, and duplicating that
rule in two routers is how the two copies drift apart.
"""
import re

import database as db
import db_runtime
import ratelimit
import search
from fastapi import HTTPException


async def resolve_film_id(src: str, ref: str, *, user_id: int | None = None) -> int:
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
