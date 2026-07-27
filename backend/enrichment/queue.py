"""Долговечная очередь заданий обогащения поверх основной БД.

Почему не Redis/Celery: в проекте их нет, а нагрузка — десятки заданий в сутки
на каталог в сотни фильмов. Postgres умеет ровно то, что здесь нужно, через
``FOR UPDATE SKIP LOCKED``, и это не добавляет ни инфраструктуры, ни счёта.

Почему не FastAPI BackgroundTasks: веб-процесс может перезапуститься сразу после
ответа, и задание исчезнет вместе с ним. Долговечное состояние обязано лежать в
базе.

Локально проект работает на SQLite, где писатель всегда один, — там тот же
захват делается без SKIP LOCKED, но с тем же контрактом «одно задание одному
воркеру».
"""
from __future__ import annotations

import logging
import random
from datetime import UTC, datetime, timedelta

import aiosqlite
import database as db
import db_runtime

from .models import (
    JOB_COMPLETED,
    JOB_DEAD_LETTER,
    JOB_FAILED,
    JOB_FULL,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_RETRY,
    EnrichmentJob,
)

logger = logging.getLogger(__name__)

# Класс ошибки решает, повторять ли задание. Неверный id провайдера не станет
# верным от десятого повтора.
RETRYABLE_ERRORS = frozenset({"provider_timeout", "provider_rate_limit", "provider_5xx",
                              "database_unavailable", "worker_interrupted", "unknown"})
PERMANENT_ERRORS = frozenset({"invalid_provider_id", "unsupported_media_type",
                              "missing_title", "malformed_source", "validation_failed"})

STUCK_JOB_TIMEOUT_MINUTES = 20


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def retry_delay(attempt: int) -> timedelta:
    """Экспоненциальная задержка с дрожанием: одновременно упавшие задания не
    должны вернуться одной волной."""
    capped = min(max(attempt, 0), 8)
    seconds = min(30 * (2 ** capped), 6 * 60 * 60)
    return timedelta(seconds=seconds * random.uniform(0.80, 1.20))


async def enqueue(film_id: int, *, feature_version: str, taxonomy_version: str,
                  job_type: str = JOB_FULL, priority: int = 0,
                  expected_source_hash: str | None = None, connection=None) -> bool:
    """Поставить задание. Возвращает True, только если задание действительно
    создано; False — если активное задание на ту же версию уже есть.

    Одна инструкция вместо SELECT + INSERT: между проверкой и вставкой два
    процесса успевали оба не увидеть строку, и проигравший получал не «уже есть»,
    а исключение уникальности. Решает частичный уникальный индекс, а не гонка в
    коде; RETURNING отличает вставку от конфликта одинаково в SQLite и Postgres.
    """
    async def _run(conn) -> bool:
        conn.row_factory = aiosqlite.Row
        now = _now()
        cur = await conn.execute(
            "INSERT INTO movie_enrichment_jobs (film_id, job_type, status, priority, "
            "requested_feature_version, requested_taxonomy_version, expected_source_hash, "
            "run_after, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (film_id, job_type, JOB_PENDING, priority, feature_version, taxonomy_version,
             expected_source_hash, now, now, now))
        return await cur.fetchone() is not None

    if connection is not None:
        # В той же транзакции, что и сохранение фильма: либо фильм и задание,
        # либо ничего.
        return await _run(connection)
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        created = await _run(conn)
        await conn.commit()
        return created


_CLAIM_POSTGRES = """
WITH selected AS (
    SELECT id FROM movie_enrichment_jobs
    WHERE status IN ('pending','retry') AND run_after <= ?
    ORDER BY priority DESC, created_at ASC, id ASC
    -- Порядок обязателен: в PostgreSQL блокирующая клауза идёт ПОСЛЕ LIMIT.
    LIMIT ?
    FOR UPDATE SKIP LOCKED
)
UPDATE movie_enrichment_jobs AS jobs
SET status='processing', locked_at=?, locked_by=?, attempts=attempts+1, updated_at=?
FROM selected
WHERE jobs.id = selected.id
RETURNING jobs.id, jobs.film_id, jobs.job_type, jobs.status, jobs.attempts,
          jobs.max_attempts, jobs.requested_feature_version,
          jobs.requested_taxonomy_version, jobs.expected_source_hash
"""


async def claim(worker_id: str, batch_size: int = 5) -> list[EnrichmentJob]:
    """Захватить задания атомарно и сразу зафиксировать состояние.

    Транзакция закрывается ДО обращений к провайдеру и расчёта признаков: держать
    её открытой на всё время внешней работы значит занять соединение и словить
    таймаут пула на первом же медленном ответе.
    """
    now = _now()
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        if db._PG:
            rows = await (await conn.execute(
                _CLAIM_POSTGRES, (now, batch_size, now, worker_id, now))).fetchall()
        else:
            # SQLite: писатель один, поэтому выборка и пометка внутри одной
            # транзакции дают ту же гарантию без SKIP LOCKED.
            candidates = await (await conn.execute(
                "SELECT id FROM movie_enrichment_jobs WHERE status IN ('pending','retry') "
                "AND run_after <= ? ORDER BY priority DESC, created_at ASC, id ASC LIMIT ?",
                (now, batch_size))).fetchall()
            ids = [row["id"] for row in candidates]
            if not ids:
                await conn.commit()
                return []
            marks = ",".join("?" for _ in ids)
            await conn.execute(
                f"UPDATE movie_enrichment_jobs SET status='processing', locked_at=?, locked_by=?, "
                f"attempts=attempts+1, updated_at=? WHERE id IN ({marks})",
                (now, worker_id, now, *ids))
            rows = await (await conn.execute(
                f"SELECT id, film_id, job_type, status, attempts, max_attempts, "
                f"requested_feature_version, requested_taxonomy_version, expected_source_hash "
                f"FROM movie_enrichment_jobs WHERE id IN ({marks})", tuple(ids))).fetchall()
        await conn.commit()
    return [EnrichmentJob(
        id=row["id"], film_id=row["film_id"], job_type=row["job_type"], status=row["status"],
        attempts=row["attempts"], max_attempts=row["max_attempts"],
        requested_feature_version=row["requested_feature_version"],
        requested_taxonomy_version=row["requested_taxonomy_version"],
        expected_source_hash=row["expected_source_hash"]) for row in rows]


async def complete(job_id: int) -> None:
    now = _now()
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        await conn.execute(
            "UPDATE movie_enrichment_jobs SET status=?, locked_at=NULL, locked_by=NULL, "
            "completed_at=?, updated_at=?, last_error_code=NULL, last_error_message=NULL "
            "WHERE id=?", (JOB_COMPLETED, now, now, job_id))
        await conn.commit()


async def fail(job: EnrichmentJob, *, error_code: str, message: str) -> str:
    """Вернуть задание в очередь или увести в dead letter.

    Сообщение обрезаем и не кладём сюда ни токенов, ни полного стека: строка
    задания читается в админской диагностике.
    """
    retryable = error_code in RETRYABLE_ERRORS
    exhausted = job.attempts >= job.max_attempts
    if not retryable:
        status, run_after = JOB_FAILED, _now()
    elif exhausted:
        status, run_after = JOB_DEAD_LETTER, _now()
    else:
        status = JOB_RETRY
        run_after = _iso(datetime.now(UTC) + retry_delay(job.attempts))
    now = _now()
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        await conn.execute(
            "UPDATE movie_enrichment_jobs SET status=?, locked_at=NULL, locked_by=NULL, "
            "run_after=?, updated_at=?, last_error_code=?, last_error_message=? WHERE id=?",
            (status, run_after, now, error_code[:64], str(message)[:400], job.id))
        await conn.commit()
    return status


async def release_stuck(timeout_minutes: int = STUCK_JOB_TIMEOUT_MINUTES) -> int:
    """Вернуть в очередь задания умершего воркера.

    Таймаут заметно больше измеренной длительности задания (доли секунды на
    правилах), поэтому живое задание сбросить нельзя.
    """
    cutoff = _iso(datetime.now(UTC) - timedelta(minutes=timeout_minutes))
    now = _now()
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        cur = await conn.execute(
            "UPDATE movie_enrichment_jobs SET status=?, locked_at=NULL, locked_by=NULL, "
            "run_after=?, updated_at=? WHERE status=? AND locked_at IS NOT NULL AND locked_at < ?",
            (JOB_RETRY, now, now, JOB_PROCESSING, cutoff))
        await conn.commit()
        return cur.rowcount


async def stats() -> dict[str, int]:
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT status, COUNT(*) AS n FROM movie_enrichment_jobs GROUP BY status")).fetchall()
    return {row["status"]: row["n"] for row in rows}


async def retry_dead_letters(limit: int = 100) -> int:
    """Явное решение оператора: dead letter сам по себе не возвращается."""
    now = _now()
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT id FROM movie_enrichment_jobs WHERE status IN (?,?) LIMIT ?",
            (JOB_DEAD_LETTER, JOB_FAILED, limit))).fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        await conn.execute(
            f"UPDATE movie_enrichment_jobs SET status=?, attempts=0, run_after=?, updated_at=? "
            f"WHERE id IN ({marks})", (JOB_PENDING, now, now, *ids))
        await conn.commit()
        return len(ids)
