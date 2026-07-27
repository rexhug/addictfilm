"""Поиск фильмов, до которых обогащение не дошло.

Хук на создании фильма — не гарантия. Запись может приехать импортом, гонкой
или из ветки кода, которая появится позже. Согласование не полагается на хук: оно
сравнивает фактическое состояние каталога с профилями и добирает разницу.

Процесс идемпотентен: повторный запуск не создаёт вторых заданий.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiosqlite
import database as db
import db_runtime

from . import provider, queue, repository
from .models import JOB_ACTIVE_STATES, PROFILE_FAILED, PROFILE_STALE, build_source_hash
from .taxonomy import MOVIE_FEATURE_VERSION, MOVIE_TAXONOMY_VERSION, RULE_EXTRACTOR_VERSION

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationReport:
    scanned: int = 0
    missing_profile: int = 0
    stale_version: int = 0
    changed_source: int = 0
    failed_retry: int = 0
    released_stuck: int = 0
    enqueued: int = 0
    skipped_active: int = 0
    reasons: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"scanned": self.scanned, "missing_profile": self.missing_profile,
                "stale_version": self.stale_version, "changed_source": self.changed_source,
                "failed_retry": self.failed_retry, "released_stuck": self.released_stuck,
                "enqueued": self.enqueued, "skipped_active": self.skipped_active}


async def _films_batch(limit: int, offset: int) -> list[dict]:
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT * FROM films ORDER BY id LIMIT ? OFFSET ?", (limit, offset))).fetchall()
    return [dict(row) for row in rows]


async def _films_with_active_jobs() -> set[int]:
    placeholders = ",".join("?" for _ in JOB_ACTIVE_STATES)
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            f"SELECT DISTINCT film_id FROM movie_enrichment_jobs WHERE status IN ({placeholders})",
            JOB_ACTIVE_STATES)).fetchall()
    return {row["film_id"] for row in rows}


def _needs_rebuild(film: dict, profile: dict | None, override: dict | None = None) -> str | None:
    """Причина пересчёта или None. Одна функция на все вызовы — иначе фоновая
    и ручная проверки начинают расходиться."""
    if profile is None:
        return "missing_profile"
    if profile["feature_version"] != MOVIE_FEATURE_VERSION:
        return "stale_version"
    if profile["taxonomy_version"] != MOVIE_TAXONOMY_VERSION:
        return "stale_version"
    if profile["extractor_version"] != RULE_EXTRACTOR_VERSION:
        return "stale_version"
    # Устаревший и сломавшийся — разные причины: первый ждёт пересчёта после
    # смены версии, второй требует внимания человека.
    if profile["status"] == PROFILE_STALE:
        return "stale_version"
    if profile["status"] == PROFILE_FAILED:
        return "failed_retry"
    # Хеш считается ровно так же, как при обогащении, включая ручную правку:
    # иначе фильм с правкой вечно выглядел бы изменившимся и переставлялся в
    # очередь на каждом проходе.
    expected = build_source_hash({**provider.from_catalog(film).source_payload(),
                                  "override": override})
    if profile["source_hash"] != expected:
        return "changed_source"
    return None


async def reconcile(*, batch_size: int = 200, dry_run: bool = False,
                    release_stuck: bool = True) -> ReconciliationReport:
    report = ReconciliationReport()
    if release_stuck and not dry_run:
        report.released_stuck = await queue.release_stuck()
    active = await _films_with_active_jobs()

    offset = 0
    while True:
        films = await _films_batch(batch_size, offset)
        if not films:
            break
        offset += len(films)
        profiles = await repository.get_profiles([film["id"] for film in films])
        for film in films:
            report.scanned += 1
            override = await repository.get_override(film["id"]) if profiles.get(film["id"]) else None
            reason = _needs_rebuild(film, profiles.get(film["id"]), override)
            if reason is None:
                continue
            setattr(report, reason, getattr(report, reason) + 1)
            report.reasons[film["id"]] = reason
            if film["id"] in active:
                report.skipped_active += 1
                continue
            if dry_run:
                continue
            # Пропущенный профиль важнее устаревшего: без него фильм вообще не
            # участвует в подборе по настроению.
            priority = 10 if reason == "missing_profile" else 0
            if await queue.enqueue(film["id"], feature_version=MOVIE_FEATURE_VERSION,
                                   taxonomy_version=MOVIE_TAXONOMY_VERSION, priority=priority):
                report.enqueued += 1
    return report
