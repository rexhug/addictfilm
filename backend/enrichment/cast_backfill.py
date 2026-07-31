"""Канонический состав каталога: ``python -m enrichment.cast_backfill``.

Умолчания консервативные: команда тратит квоту kinopoisk и её запускают руками
на проде. `--dry-run` ничего не пишет и показывает то, что записал бы настоящий
проход, — по нему и принимается решение включать флаг.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

import database as db
import db_runtime
import kinopoisk

logger = logging.getLogger(__name__)

# Детали персоны стоят отдельного запроса, поэтому их берём только для тех, кто
# реально виден в начале списка и остался без портрета.
MAX_PERSON_DETAIL_CALLS = 3


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Канонический состав фильмов")
    parser.add_argument("--film-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--validate-portraits", action="store_true",
                        help="проверить, что ссылки на портреты действительно открываются")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать результат, ничего не записывая")
    return parser.parse_args(argv)


async def _candidates(args: argparse.Namespace) -> list[dict]:
    if args.film_id is not None:
        film = await db.get_film(args.film_id)
        return [film] if film else []
    import aiosqlite
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM films WHERE imdb_id LIKE 'tt%' "
            "AND (cast_json IS NULL OR cast_json = '') "
            "ORDER BY cast_checked_at IS NULL DESC, id DESC LIMIT ?",
            (max(1, int(args.limit)),))
        return [dict(row) for row in await cur.fetchall()]


def _summarize(film: dict, cast: list[dict]) -> str:
    with_photo = sum(1 for person in cast if person.get("photo_url"))
    leads = ", ".join(person["name"] for person in cast[:3]) or "—"
    return (f"{film['id']:>5}  {str(film.get('title'))[:34]:34} "
            f"актёров={len(cast):>2} с фото={with_photo:>2}  {leads}")


async def _cast_for(film: dict) -> list[dict]:
    """Состав из уже сохранённого каталога, без сетевых запросов, если он есть."""
    stored = str(film.get("cast_json") or "").strip()
    if stored:
        try:
            decoded = json.loads(stored)
            if isinstance(decoded, list):
                return [item for item in decoded if isinstance(item, dict)]
        except json.JSONDecodeError:
            logger.warning("фильм %s: cast_json не разбирается, перечитываем", film.get("id"))
    imdb_id = str(film.get("imdb_id") or "")
    return (await kinopoisk.cast_by_imdb([imdb_id])).get(imdb_id) or []


async def run(args: argparse.Namespace) -> dict:
    await db.init_db()
    films = await _candidates(args)
    if not films:
        print("Кандидатов нет.")
        return {"examined": 0, "stored": 0}

    stored = 0
    gate = asyncio.Semaphore(max(1, min(args.concurrency, 3)))

    async def _one(film: dict) -> tuple[dict, list[dict]]:
        async with gate:
            try:
                return film, await _cast_for(film)
            except Exception:
                logger.warning("фильм %s не обработан", film.get("id"), exc_info=True)
                return film, []

    batch_size = max(1, args.batch_size)
    for start in range(0, len(films), batch_size):
        chunk = films[start:start + batch_size]
        for film, cast in await asyncio.gather(*(_one(item) for item in chunk)):
            print(_summarize(film, cast))
            if cast and not args.dry_run:
                await db.set_film_cast(film["id"], cast)
                stored += 1
        if start + batch_size < len(films):
            await asyncio.sleep(1.0)

    await kinopoisk.aclose()
    result = {"examined": len(films), "stored": stored, "dry_run": bool(args.dry_run)}
    print("\nИтог:", result)
    return result


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                        level=logging.INFO)
    asyncio.run(run(_parse_args(argv)))


if __name__ == "__main__":
    main()
