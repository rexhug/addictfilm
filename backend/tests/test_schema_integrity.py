import tempfile
import unittest
from pathlib import Path

import aiosqlite
import database as db
import db_runtime


class FreshSchemaIntegrityTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_fresh_schema_contains_role_and_relationship_constraints(self):
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            users = await (await conn.execute("PRAGMA table_info(users)")).fetchall()
            user_films_fks = await (await conn.execute("PRAGMA foreign_key_list(user_films)")).fetchall()

        self.assertIn("role", {row[1] for row in users})
        self.assertEqual({row[2] for row in user_films_fks}, {"users", "films"})

    async def test_foreign_keys_reject_orphaned_user_film_rows(self):
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            with self.assertRaises(aiosqlite.IntegrityError) as error:
                await conn.execute(
                    "INSERT INTO user_films (user_id, film_id, status) VALUES (?,?,?)",
                    (999, 999, "want_to_watch"),
                )

        self.assertIn("FOREIGN KEY", str(error.exception).upper())

    async def test_migration_adds_role_and_preserves_latest_pending_invite(self):
        # Simulate a database created before role support and before the pending
        # invite uniqueness invariant existed.
        self.temp_dir.cleanup()
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.temp_dir.name) / "legacy.db")
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.executescript("""
                CREATE TABLE users (
                    id BIGINT PRIMARY KEY,
                    first_name TEXT,
                    username TEXT,
                    created_at TEXT,
                    last_seen TEXT
                );
                CREATE TABLE partner_invites (
                    token TEXT PRIMARY KEY,
                    from_user BIGINT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL
                );
                INSERT INTO users VALUES (1, 'One', NULL, '2026-01-01', '2026-01-01');
                INSERT INTO partner_invites VALUES ('old', 1, 'pending', '2026-01-01T00:00:00+00:00');
                INSERT INTO partner_invites VALUES ('new', 1, 'pending', '2026-01-02T00:00:00+00:00');
            """)
            await conn.commit()

        await db.init_db()

        self.assertIsNone(await db.get_user_role(1))
        self.assertEqual(await db.get_pending_invite(1), "new")
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            cur = await conn.execute("SELECT status FROM partner_invites WHERE token = 'old'")
            self.assertEqual((await cur.fetchone())[0], "superseded")
