"""Regression coverage for request-local PostgreSQL savepoints."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database as db
import db_runtime
import main


class NotificationFailureRegressionTests(unittest.IsolatedAsyncioTestCase):
    """A best-effort notification failure never loses the primary rating."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "notification.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "Tester", "username": "tester"})
        self.film_id = await db.get_or_create_film("tt9910001", "Notification failure")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def test_rate_survives_notification_failure(self):
        async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
            with patch.object(
                main.pair_activity_notifications,
                "notify_partner_film_rated",
                AsyncMock(side_effect=RuntimeError("notification database failure")),
            ):
                response = await main.rate(
                    self.film_id, main.RateBody(rating=8), {"id": 1},
                )

        self.assertEqual(response, {"ok": True})
        saved = await db.get_user_film(1, self.film_id)
        self.assertEqual(saved["rating"], 8)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL", "").strip(), "requires TEST_DATABASE_URL")
class PostgresNestedTransactionIsolationTests(unittest.IsolatedAsyncioTestCase):
    """These checks run only against the same PostgreSQL service used by CI."""

    async def asyncSetUp(self):
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        await db_runtime.close()
        db.DATABASE_URL = os.environ["TEST_DATABASE_URL"].strip()
        db._PG = True
        await db.init_db()
        self.ids = (900000001, 900000002, 900000003, 900000004)

    async def asyncTearDown(self):
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "DELETE FROM users WHERE id IN (?,?,?,?)", self.ids,
            )
            await conn.commit()
        await db_runtime.close()
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg

    async def _user(self, user_id: int) -> None:
        await db.upsert_user({"id": user_id, "first_name": str(user_id), "username": None})

    async def _exists(self, user_id: int) -> bool:
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT 1 FROM users WHERE id = ?", (user_id,),
            )).fetchone()
        return row is not None

    async def test_caught_nested_failure_preserves_outer_writes(self):
        first, failed, last = self.ids[:3]
        async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
            await self._user(first)
            with self.assertRaisesRegex(RuntimeError, "nested failure"):
                async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
                    await conn.execute(
                        "INSERT INTO users (id, first_name, created_at, last_seen) "
                        "VALUES (?,?,?,?)",
                        (failed, "failed", "now", "now"),
                    )
                    raise RuntimeError("nested failure")
            await self._user(last)

        self.assertTrue(await self._exists(first))
        self.assertFalse(await self._exists(failed))
        self.assertTrue(await self._exists(last))

    async def test_uncaught_failure_rolls_back_outer_transaction(self):
        first, failed = self.ids[2:]
        with self.assertRaisesRegex(RuntimeError, "abort request"):
            async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
                await self._user(first)
                async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
                    await conn.execute(
                        "INSERT INTO users (id, first_name, created_at, last_seen) "
                        "VALUES (?,?,?,?)",
                        (failed, "failed", "now", "now"),
                    )
                    raise RuntimeError("abort request")

        self.assertFalse(await self._exists(first))
        self.assertFalse(await self._exists(failed))

    async def test_nested_read_does_not_open_savepoint(self):
        async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
            async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
                self.assertIsNotNone(await (await conn.execute("SELECT 1")).fetchone())
            owner = db_runtime.current_connection()._connection._owner
            self.assertFalse(owner.has_transaction)
