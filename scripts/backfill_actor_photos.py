#!/usr/bin/env python3
"""Бекфіл фото акторів для фільмів каталога без actors_photos.

kinopoisk.dev віддає persons ціликом (вкл. photo) одним батч-запитом на 30 imdb id.
Синтетичні kp_<id> тягнуться поштучно через get_movie.

Ідемпотентно: не затирає вже наявні фото, бере лише фільми з порожнім actors_photos.
Запускай повторно, доки `remaining` не впаде до 0.

Використання:
  python3 scripts/backfill_actor_photos.py                 # один прогін
  python3 scripts/backfill_actor_photos.py --limit 500     # відсканувати більше
  python3 scripts/backfill_actor_photos.py --all           # ганяти, доки є прогрес
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import database as db  # noqa: E402
import kinopoisk  # noqa: E402
import posters  # noqa: E402


async def _run(limit: int, run_all: bool) -> None:
    await db.init_db()
    total = {"scanned": 0, "updated": 0}
    try:
        while True:
            stats = await posters.backfill_actor_photos(limit=limit)
            total["scanned"] += stats["scanned"]
            total["updated"] += stats["updated"]
            print(f"прогон: скан={stats['scanned']} обновлено={stats['updated']} "
                  f"осталось={stats['remaining']}")
            if not run_all or stats["updated"] == 0:
                break
    finally:
        await kinopoisk.aclose()
    print(f"\nИТОГО: обновлено={total['updated']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200, help="сколько фильмов сканировать за прогон")
    ap.add_argument("--all", action="store_true", help="повторять прогоны, пока есть прогресс")
    args = ap.parse_args()
    asyncio.run(_run(args.limit, args.all))


if __name__ == "__main__":
    main()
