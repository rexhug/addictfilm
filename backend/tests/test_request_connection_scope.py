import asyncio
import os
import tempfile
import unittest
from pathlib import Path

import database as db
import db_runtime


class RequestConnectionScopeTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_database_calls_share_one_connection_inside_scope(self):
        async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
            first = db_runtime.current_connection()
            await db.upsert_user({"id": 1, "first_name": "One", "username": None})
            second = db_runtime.current_connection()

        self.assertIsNotNone(first)
        self.assertIs(first, second)

    async def test_exception_rolls_back_all_writes_in_scope(self):
        with self.assertRaisesRegex(RuntimeError, "abort request"):
            async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
                await db.upsert_user({"id": 1, "first_name": "One", "username": None})
                await db.upsert_user({"id": 2, "first_name": "Two", "username": None})
                raise RuntimeError("abort request")

        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as connection:
            row = await (await connection.execute("SELECT COUNT(*) FROM users")).fetchone()
        self.assertEqual(row[0], 0)

    async def test_background_task_does_not_inherit_request_connection(self):
        opened = []
        original_connect = db_runtime.aiosqlite.connect

        def tracked_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        db_runtime.aiosqlite.connect = tracked_connect
        started = asyncio.Event()
        release = asyncio.Event()

        async def write_after_scope_exits():
            child_connection = db_runtime.current_connection()
            started.set()
            await release.wait()
            await db.upsert_user({"id": 99, "first_name": "Child", "username": None})
            return child_connection

        try:
            async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
                parent = db_runtime.current_connection()
                await parent.execute("SELECT 1")
                parent_owner = parent._connection._owner
                task = db_runtime.create_background_task(
                    write_after_scope_exits(), name="scope-test",
                )
                await started.wait()

            self.assertTrue(parent._connection._closed)
            with self.assertRaisesRegex(RuntimeError, "already closed"):
                await parent.execute("SELECT 1")

            release.set()
            child = await task

            self.assertIsNotNone(parent)
            self.assertIsNone(child)
            self.assertIsNotNone(await db.get_user(99))
            self.assertIs(parent_owner, opened[0])
            self.assertIsNot(parent_owner, opened[1])
        finally:
            db_runtime.aiosqlite.connect = original_connect

    async def test_database_calls_without_scope_keep_cli_path(self):
        self.assertIsNone(db_runtime.current_connection())
        await db.upsert_user({"id": 7, "first_name": "Seven", "username": None})
        self.assertIsNotNone(await db.get_user(7))


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL", "").strip(), "requires TEST_DATABASE_URL")
class PostgresBackgroundConnectionScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        await db_runtime.close()
        db.DATABASE_URL = os.environ["TEST_DATABASE_URL"].strip()
        db._PG = True
        await db.init_db()
        self.user_id = 900000099

    async def asyncTearDown(self):
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute("DELETE FROM users WHERE id = ?", (self.user_id,))
            await conn.commit()
        await db_runtime.close()
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg

    async def test_background_task_cannot_write_through_closed_request_connection(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def write_after_scope_exits():
            inherited = db_runtime.current_connection()
            started.set()
            await release.wait()
            async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
                await conn.execute(
                    "INSERT INTO users (id, first_name, created_at, last_seen) VALUES (?,?,?,?)",
                    (self.user_id, "Child", "now", "now"),
                )
                await conn.commit()
                return inherited, conn

        async with db_runtime.request_scope(db.DB_PATH, db.DATABASE_URL):
            parent = db_runtime.current_connection()
            await parent.execute("SELECT 1")
            parent_owner = parent._connection._owner
            task = db_runtime.create_background_task(
                write_after_scope_exits(), name="postgres-scope-test",
            )
            await started.wait()

        self.assertTrue(parent._connection._closed)
        with self.assertRaisesRegex(RuntimeError, "already closed"):
            await parent.execute("SELECT 1")

        release.set()
        inherited, child_owner = await task
        self.assertIsNone(inherited)
        self.assertIsNot(parent_owner, child_owner)
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT 1 FROM users WHERE id = ?", (self.user_id,),
            )).fetchone()
        self.assertIsNotNone(row)
