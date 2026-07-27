"""Разовое добитие films.media_type для записей, приехавших без типа.

Колонку заполняет провайдер при сохранении, но каталог старше колонки: часть
записей пришла из старого бота и из ранних версий поиска. Пока тип неизвестен,
подбор ФИЛЬМОВ вынужден пропускать такие записи, и в выдачу попадают сериалы.

Источники строго те же, что и в бою: OMDb ``Type`` по настоящему imdb-id
(дешёвый и точный) и kinopoisk ``type`` для записей, у которых есть только
kp_id. Скрипт идемпотентен — уже заполненные строки не трогает.

    python scripts/backfill_media_type.py [--limit N] [--dry-run]
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import aiosqlite  # noqa: E402
import database as db  # noqa: E402
import db_runtime  # noqa: E402
import kinopoisk  # noqa: E402
import media_type  # noqa: E402
import omdb  # noqa: E402
from config import DATABASE_URL  # noqa: E402


async def _pending(limit: int) -> list[dict]:
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT id, imdb_id, kp_id, title, genres FROM films "
            "WHERE media_type IS NULL OR media_type = '' ORDER BY id LIMIT ?", (limit,))
        return [dict(row) for row in await cur.fetchall()]


async def _resolve(film: dict) -> tuple[str, str]:
    """(тип, источник). UNKNOWN означает «провайдер не ответил», а не «фильм»."""
    imdb_id = str(film.get("imdb_id") or "")
    if imdb_id.startswith("tt"):
        data = await omdb.get_movie(imdb_id)
        if data:
            resolved = media_type.from_omdb(data)
            if resolved != media_type.UNKNOWN:
                return resolved, "omdb"
    if film.get("kp_id"):
        doc = await kinopoisk.get_movie(str(film["kp_id"]))
        if doc:
            resolved = media_type.from_kinopoisk(doc)
            if resolved != media_type.UNKNOWN:
                return resolved, "kinopoisk"
    return media_type.UNKNOWN, "none"


async def _store(film_id: int, value: str, source: str) -> None:
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        await conn.execute("UPDATE films SET media_type = ?, media_type_source = ? WHERE id = ?",
                           (value, source, film_id))
        await conn.commit()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pending = await _pending(args.limit)
    print(f"без типа: {len(pending)}")
    counts: dict[str, int] = {}
    for film in pending:
        value, source = await _resolve(film)
        counts[value] = counts.get(value, 0) + 1
        if value != media_type.UNKNOWN and not args.dry_run:
            await _store(film["id"], value, source)
        if value != media_type.MOVIE:
            print(f"  {value:8} {source:9} {str(film['title'])[:44]}")
    await kinopoisk.aclose()
    await omdb.aclose()
    print("итог:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    asyncio.run(main())
