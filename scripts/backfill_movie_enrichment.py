"""Обогащение существующего каталога — разово и с отчётом.

Не запускается из веб-запроса: обход всего каталога не должен зависеть от
таймаута HTTP. Идемпотентен — повторный прогон не пересчитывает то, что
не изменилось.

    python scripts/backfill_movie_enrichment.py --dry-run
    python scripts/backfill_movie_enrichment.py --only-missing --batch-size 50
    python scripts/backfill_movie_enrichment.py --without-semantic
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import aiosqlite
import database as db
import db_runtime
from enrichment import reconciliation, repository, service
from enrichment.models import JOB_FULL, PROFILE_FAILED, EnrichmentJob
from enrichment.semantic import DisabledSemanticClassifier, build_classifier
from enrichment.taxonomy import MOVIE_FEATURE_VERSION, MOVIE_TAXONOMY_VERSION


async def _films(movie_id: int | None) -> list[dict]:
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        if movie_id:
            rows = await (await conn.execute("SELECT * FROM films WHERE id=?", (movie_id,))).fetchall()
        else:
            rows = await (await conn.execute("SELECT * FROM films ORDER BY id")).fetchall()
    return [dict(row) for row in rows]


def _fake_job(film_id: int) -> EnrichmentJob:
    """Бэкфил идёт мимо очереди: он и есть массовая обработка."""
    return EnrichmentJob(id=0, film_id=film_id, job_type=JOB_FULL, status="processing",
                         attempts=1, max_attempts=1,
                         requested_feature_version=MOVIE_FEATURE_VERSION,
                         requested_taxonomy_version=MOVIE_TAXONOMY_VERSION)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feature-version", default=MOVIE_FEATURE_VERSION)
    parser.add_argument("--taxonomy-version", default=MOVIE_TAXONOMY_VERSION)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--movie-id", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--only-stale", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--without-semantic", action="store_true")
    parser.add_argument("--without-provider", action="store_true",
                        help="не тратить бюджет вызовов провайдера")
    args = parser.parse_args()

    await db.init_db()
    classifier = DisabledSemanticClassifier() if args.without_semantic else build_classifier()
    films = await _films(args.movie_id)
    profiles = await repository.get_profiles([film["id"] for film in films])

    counters = {"scanned": 0, "recalculated": 0, "unchanged": 0, "failed": 0,
                "ready": 0, "low_confidence": 0, "non_movie": 0, "skipped_filter": 0}
    started = time.monotonic()
    for index, film in enumerate(films, 1):
        counters["scanned"] += 1
        profile = profiles.get(film["id"])
        reason = reconciliation._needs_rebuild(film, profile)
        if args.only_missing and reason != "missing_profile":
            counters["skipped_filter"] += 1
            continue
        if args.only_stale and reason != "stale_version":
            counters["skipped_filter"] += 1
            continue
        if args.retry_failed and not (profile and profile["status"] == PROFILE_FAILED):
            counters["skipped_filter"] += 1
            continue
        if reason is None and not args.retry_failed:
            counters["unchanged"] += 1
            continue
        if args.dry_run:
            counters["recalculated"] += 1
            continue
        try:
            result = await service.process_job(_fake_job(film["id"]), classifier=classifier,
                                               fetch_provider=not args.without_provider)
        except Exception as error:
            code, message = service.classify_error(error)
            counters["failed"] += 1
            print(f"  ОШИБКА {code}: {str(film['title'])[:44]} — {message[:80]}")
            continue
        if result["skipped"]:
            counters["unchanged"] += 1
            continue
        counters["recalculated"] += 1
        counters[result["status"]] = counters.get(result["status"], 0) + 1
        if result.get("content_type") not in (None, "movie"):
            counters["non_movie"] += 1
        if index % args.batch_size == 0:
            print(f"  … {index}/{len(films)}")

    distribution = await repository.distribution()
    print(f"\nкаталог: {counters['scanned']}")
    for key in ("recalculated", "unchanged", "skipped_filter", "failed", "non_movie"):
        print(f"  {key}: {counters[key]}")
    print(f"  длительность: {time.monotonic() - started:.1f} c")
    print(f"профили: {distribution['by_status']}")
    print(f"типы записей: {distribution['by_content_type']}")
    print(f"средняя уверенность: {distribution['average_confidence']}")


if __name__ == "__main__":
    asyncio.run(main())
