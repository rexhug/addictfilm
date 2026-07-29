"""Что процесс оставляет за собой: память, строки в таблицах, живые задачи.

Ни один из этих дефектов не ломает ответ прямо сейчас — они копятся. Именно
поэтому их некому заметить без теста: приложение работает ровно до того дня,
когда перестаёт.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import aiosqlite
import database as db
import db_runtime
import fanart
import main


class PickLockTests(unittest.IsolatedAsyncioTestCase):
    """Замок на пользователя жил вечно: словарь рос на каждого нового и не
    уменьшался никогда."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "locks.db")
        db.DATABASE_URL, db._PG = "", False
        db._pick_locks.clear()
        db._pick_lock_users.clear()

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def test_a_finished_pick_leaves_no_trace(self):
        for user_id in range(50):
            async with db.recommendation_pick_lock(user_id):
                pass
        self.assertEqual(db._pick_locks, {})
        self.assertEqual(db._pick_lock_users, {})

    async def test_the_lock_still_serializes_two_concurrent_picks(self):
        """Уборка не должна выдать второму запросу ДРУГОЙ замок."""
        live, peak = 0, 0

        async def _pick():
            nonlocal live, peak
            async with db.recommendation_pick_lock(1):
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0)      # точка, где корутины расходятся
                live -= 1

        await asyncio.gather(*(_pick() for _ in range(6)))
        self.assertEqual(peak, 1, "внутрь замка одновременно вошли двое")
        self.assertEqual(db._pick_locks, {})


class RecommendationHistoryRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "history.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})
        self.film_id = await db.get_or_create_film(
            imdb_id="tt1", title="Фильм", genres="драма", media_type="movie",
            poster_url="https://p/1.jpg")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _row(self, action: str, *, age_days: int) -> None:
        moment = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "INSERT INTO recommendation_history (user_id, film_id, mode, action, created_at) "
                "VALUES (?,?,?,?,?)", (1, self.film_id, "random", action, moment))
            await conn.commit()

    async def _count(self, action: str | None = None) -> int:
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            conn.row_factory = aiosqlite.Row
            query = "SELECT COUNT(*) AS n FROM recommendation_history"
            params: tuple = ()
            if action:
                query += " WHERE action = ?"
                params = (action,)
            return int((await (await conn.execute(query, params)).fetchone())["n"])

    async def test_a_rejection_is_never_forgotten(self):
        """«Не предлагать» — постоянное решение человека о фильме, а не след
        показа: сроку хранения оно не подчиняется."""
        await self._row("rejected", age_days=3650)
        self.assertEqual(await db.prune_recommendation_history(), 0)
        self.assertEqual(await self._count("rejected"), 1)

    async def test_old_shows_are_removed_and_recent_ones_stay(self):
        await self._row("shown", age_days=200)
        await self._row("shown", age_days=3)
        await self._row("opened", age_days=200)
        removed = await db.prune_recommendation_history()
        self.assertEqual(removed, 2)
        self.assertEqual(await self._count(), 1)

    async def test_the_anti_repeat_window_is_never_touched(self):
        """Анти-повтор смотрит на показы за 7 дней — срок хранения обязан быть
        кратно больше, иначе уборка начала бы возвращать уже показанное."""
        await self._row("shown", age_days=db.RECOMMENDATION_HISTORY_RETENTION_DAYS - 1)
        self.assertEqual(await db.prune_recommendation_history(), 0)
        self.assertGreater(db.RECOMMENDATION_HISTORY_RETENTION_DAYS, 7 * 10)


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    """Три набора фоновых задач гасились по-разному: третий не гасился вовсе."""

    async def test_every_background_task_registry_is_cancelled(self):
        registries = ("_visual_enrichment_tasks", "_people_enrichment_tasks",
                      "_director_profile_enrichment_tasks")
        started: list[asyncio.Task] = []

        async def _forever():
            await asyncio.sleep(3600)

        for name in registries:
            task = asyncio.create_task(_forever())
            getattr(main, name).add(task)
            started.append(task)
        main._director_profile_enrichment_users.add(1)
        await asyncio.sleep(0)

        with mock.patch.object(main.pair_notifications, "drain", new=mock.AsyncMock()), \
             mock.patch.object(main.db_runtime, "close", new=mock.AsyncMock()):
            await main.shutdown()

        for name, task in zip(registries, started, strict=True):
            self.assertTrue(task.cancelled() or task.done(), f"{name}: задача пережила остановку")
            self.assertEqual(len(getattr(main, name)), 0, name)
        self.assertEqual(main._director_profile_enrichment_users, set())


class WorkerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_worker_closes_the_fanart_session_it_owns(self):
        """Сессия принадлежит процессу: закрывает её тот, кто его останавливает."""
        from enrichment import worker

        closed = mock.AsyncMock()
        with mock.patch.object(worker.db, "init_db", new=mock.AsyncMock()), \
             mock.patch.object(worker.reconciliation, "reconcile",
                               new=mock.AsyncMock(return_value=mock.Mock(as_dict=lambda: {}))), \
             mock.patch.object(worker.queue, "claim", new=mock.AsyncMock(return_value=[])), \
             mock.patch.object(worker.queue, "stats", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(worker.repository, "distribution",
                               new=mock.AsyncMock(return_value={"by_status": {}})), \
             mock.patch.object(worker.repository, "touch_heartbeat", new=mock.AsyncMock()), \
             mock.patch.object(fanart, "close", new=closed):
            await worker.run_worker(max_cycles=1)
        closed.assert_awaited()


class ContentSecurityPolicyTests(unittest.TestCase):
    def test_images_are_allowed_only_from_our_own_origin(self):
        """Все картинки идут через собственный /img с allowlist хостов. Открытый
        https: держал бы канал для трекинг-пикселя на случай будущего XSS."""
        self.assertIn("img-src 'self' data:", main._HTML_CSP)
        self.assertNotIn("img-src 'self' https:", main._HTML_CSP)

    def test_the_frontend_really_needs_no_external_image_host(self):
        root = Path(__file__).resolve().parents[2] / "frontend"
        sources = "\n".join((root / name).read_text()
                            for name in ("app.js", "style.css", "index.html"))
        for line in sources.splitlines():
            if "https://" not in line or "telegram.org/js" in line:
                continue
            self.assertNotRegex(
                line, r'(src|background-image)\s*[=:]\s*["\']?https://',
                "картинка грузится мимо /img — CSP её заблокирует")


if __name__ == "__main__":
    unittest.main()
