import tempfile
import unittest
from pathlib import Path

import aiosqlite
import database as db
import db_runtime


class RequestScopeLazinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def _count_connections(self):
        calls = [0]
        original = db_runtime.aiosqlite.connect

        def counted(*args, **kwargs):
            calls[0] += 1
            return original(*args, **kwargs)

        db_runtime.aiosqlite.connect = counted
        return calls, original

    async def test_scope_does_not_acquire_for_healthz_or_img_like_no_db_work(self):
        calls, original = await self._count_connections()
        try:
            async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
                pass
            self.assertEqual(calls[0], 0)

            async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
                # The image proxy streams an upstream response and does not use DB.
                pass
            self.assertEqual(calls[0], 0)
        finally:
            db_runtime.aiosqlite.connect = original

    async def test_database_endpoint_acquires_exactly_once(self):
        calls, original = await self._count_connections()
        try:
            async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
                connection = db_runtime.current_connection()
                await connection.execute("SELECT 1")
                await connection.execute("SELECT 1")
            self.assertEqual(calls[0], 1)
        finally:
            db_runtime.aiosqlite.connect = original

    async def test_row_factory_assignment_does_not_acquire(self):
        calls, original = await self._count_connections()
        try:
            async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL) as connection:
                connection.row_factory = aiosqlite.Row
                self.assertEqual(calls[0], 0)
        finally:
            db_runtime.aiosqlite.connect = original
