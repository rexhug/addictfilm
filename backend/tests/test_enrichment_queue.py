"""Долговечная очередь: дубли, захват, повторы, мёртвые письма, зависшие задания.

Локально это SQLite, поэтому конкурентность здесь проверяется по контракту
(«захваченное задание не выдаётся второй раз»), а не по поведению SKIP LOCKED.
Реальная блокировка Postgres покрыта в test_postgres_contract.py, который CI
гоняет на настоящей базе.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import database as db
import db_runtime
from config import DATABASE_URL
from enrichment import queue
from enrichment.models import JOB_DEAD_LETTER, JOB_FAILED, JOB_PENDING, JOB_RETRY
from enrichment.taxonomy import MOVIE_FEATURE_VERSION, MOVIE_TAXONOMY_VERSION


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "q.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        self.film_id = await db.get_or_create_film(
            imdb_id="tt1", title="Фильм", genres="драма", runtime="100 мин",
            poster_url="https://example.test/p.jpg", media_type="movie")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _enqueue(self, **kwargs):
        return await queue.enqueue(
            self.film_id, feature_version=kwargs.pop("feature_version", MOVIE_FEATURE_VERSION),
            taxonomy_version=MOVIE_TAXONOMY_VERSION, **kwargs)

    async def _count(self) -> int:
        async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT COUNT(*) AS n FROM movie_enrichment_jobs")).fetchone()
            return row[0]

    async def test_creating_a_film_enqueues_exactly_one_job(self):
        """Хук на сохранении каталога — а не отдельный ручной вызов."""
        self.assertEqual(await self._count(), 1)

    async def test_duplicate_enqueue_is_idempotent(self):
        self.assertFalse(await self._enqueue())
        self.assertEqual(await self._count(), 1)

    async def test_new_feature_version_creates_a_new_job(self):
        self.assertTrue(await self._enqueue(feature_version="movie-mood-v99"))
        self.assertEqual(await self._count(), 2)

    async def test_claimed_job_is_not_handed_to_a_second_worker(self):
        first = await queue.claim("worker-a", batch_size=10)
        second = await queue.claim("worker-b", batch_size=10)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    async def test_claim_marks_processing_and_counts_the_attempt(self):
        job = (await queue.claim("worker-a"))[0]
        self.assertEqual(job.status, "processing")
        self.assertEqual(job.attempts, 1)

    async def test_completed_job_is_never_reclaimed(self):
        job = (await queue.claim("worker-a"))[0]
        await queue.complete(job.id)
        self.assertEqual(await queue.claim("worker-b"), [])
        self.assertEqual((await queue.stats()).get("completed"), 1)

    async def test_retryable_failure_returns_the_job_with_backoff(self):
        job = (await queue.claim("worker-a"))[0]
        status = await queue.fail(job, error_code="provider_timeout", message="таймаут")
        self.assertEqual(status, JOB_RETRY)
        # Повтор назначен в будущее — очередь не крутит одно и то же в цикле.
        self.assertEqual(await queue.claim("worker-b"), [])

    async def test_permanent_failure_is_not_retried(self):
        job = (await queue.claim("worker-a"))[0]
        status = await queue.fail(job, error_code="invalid_provider_id", message="нет такого id")
        self.assertEqual(status, JOB_FAILED)

    async def test_exhausted_attempts_go_to_dead_letter(self):
        job = (await queue.claim("worker-a"))[0]
        exhausted = type(job)(**{**job.__dict__, "attempts": job.max_attempts})
        self.assertEqual(await queue.fail(exhausted, error_code="provider_timeout", message="x"),
                         JOB_DEAD_LETTER)

    async def test_backoff_is_bounded_and_grows(self):
        self.assertLess(queue.retry_delay(0), queue.retry_delay(4))
        self.assertLessEqual(queue.retry_delay(50), timedelta(hours=6) * 1.21)

    async def test_stuck_job_of_a_dead_worker_is_released(self):
        job = (await queue.claim("worker-a"))[0]
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
            await conn.execute("UPDATE movie_enrichment_jobs SET locked_at=? WHERE id=?",
                               (stale, job.id))
            await conn.commit()
        self.assertEqual(await queue.release_stuck(), 1)
        self.assertEqual(len(await queue.claim("worker-b")), 1)

    async def test_recently_locked_job_is_not_released_too_early(self):
        await queue.claim("worker-a")
        self.assertEqual(await queue.release_stuck(), 0)

    async def test_dead_letters_return_only_by_explicit_decision(self):
        job = (await queue.claim("worker-a"))[0]
        exhausted = type(job)(**{**job.__dict__, "attempts": job.max_attempts})
        await queue.fail(exhausted, error_code="provider_timeout", message="x")
        self.assertEqual(await queue.claim("worker-b"), [])
        self.assertEqual(await queue.retry_dead_letters(), 1)
        self.assertEqual((await queue.stats()).get(JOB_PENDING), 1)

    async def test_error_message_is_truncated(self):
        job = (await queue.claim("worker-a"))[0]
        await queue.fail(job, error_code="unknown", message="x" * 5000)
        async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT last_error_message FROM movie_enrichment_jobs WHERE id=?",
                (job.id,))).fetchone()
        self.assertLessEqual(len(row[0]), 400)


if __name__ == "__main__":
    unittest.main()
