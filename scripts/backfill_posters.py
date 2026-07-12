#!/usr/bin/env python3
"""Бекфіл постерів для фільмів каталога без картинки.

Мульти-джерельний резолвер (backend/posters.py): kinopoisk.dev по imdb_id →
OMDb з апскейлом якості. Кінопоиск добирається масово (батчі), OMDb — поштучно
з лімітом за прогін, щоб не з'їсти денну квоту.

Ідемпотентно: не затирає вже наявні постери, бере лише фільми з порожнім
poster_url. Запускай повторно, доки `remaining` не впаде до 0.

Використання:
  python3 scripts/backfill_posters.py                 # один прогін, ліміт за замовчуванням
  python3 scripts/backfill_posters.py --limit 500     # відсканувати більше фільмів
  python3 scripts/backfill_posters.py --omdb-cap 100  # дозволити більше запитів до OMDb
  python3 scripts/backfill_posters.py --all           # ганяти прогони, доки лишаються без постера
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import database as db  # noqa: E402
import posters  # noqa: E402
import kinopoisk  # noqa: E402
import omdb  # noqa: E402


async def _run(limit: int, omdb_cap: int, run_all: bool) -> None:
    await db.init_db()
    total = {"scanned": 0, "kinopoisk": 0, "omdb": 0, "by_name": 0, "updated": 0}
    try:
        while True:
            stats = await posters.backfill(limit=limit, _omdb_cap=omdb_cap)
            for k in total:
                total[k] += stats[k]
            print(f"прогон: скан={stats['scanned']} КП={stats['kinopoisk']} "
                  f"OMDb={stats['omdb']} поназв={stats['by_name']} "
                  f"обновлено={stats['updated']} осталось={stats['remaining']}")
            if not run_all or stats["updated"] == 0:
                break
    finally:
        await kinopoisk.aclose()
        await omdb.aclose()
    print(f"\nИТОГО: обновлено={total['updated']} "
          f"(КП={total['kinopoisk']}, OMDb={total['omdb']}, поназв={total['by_name']})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200, help="сколько фильмов сканировать за прогон")
    ap.add_argument("--omdb-cap", type=int, default=60, help="макс. запросов к OMDb за прогон")
    ap.add_argument("--all", action="store_true", help="повторять прогоны, пока есть прогресс")
    args = ap.parse_args()
    asyncio.run(_run(args.limit, args.omdb_cap, args.all))


if __name__ == "__main__":
    main()
