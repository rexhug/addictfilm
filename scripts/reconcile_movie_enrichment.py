"""Найти фильмы без профиля, с устаревшей версией или изменившимся источником.

    python scripts/reconcile_movie_enrichment.py --dry-run
    python scripts/reconcile_movie_enrichment.py --batch-size 200
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import database as db  # noqa: E402
from enrichment import queue, reconciliation, repository  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    await db.init_db()
    report = await reconciliation.reconcile(batch_size=args.batch_size, dry_run=args.dry_run)
    print("согласование:", report.as_dict())
    print("очередь:", await queue.stats())
    print("профили:", (await repository.distribution())["by_status"])


if __name__ == "__main__":
    asyncio.run(main())
