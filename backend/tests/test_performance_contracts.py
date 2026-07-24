import tempfile
import unittest
from pathlib import Path

import database as db


class PerformanceContractsTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for optimizations that must not change product data."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        db._invalidate_genres_cache()
        await db.init_db()

    async def asyncTearDown(self):
        db._invalidate_genres_cache()
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def test_user_seen_timestamp_is_not_rewritten_for_every_request(self):
        user = {"id": 42, "first_name": "Denys", "username": "denys", "photo_url": None}
        await db.upsert_user(user)
        async with db.db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            before = (await (await conn.execute("SELECT last_seen FROM users WHERE id = ?", (42,))).fetchone())[0]
        await db.upsert_user(user)
        async with db.db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            after = (await (await conn.execute("SELECT last_seen FROM users WHERE id = ?", (42,))).fetchone())[0]
        self.assertEqual(before, after)

    async def test_genre_projection_is_exact_cached_and_invalidated_on_catalog_insert(self):
        action = await db.get_or_create_film("tt0000001", "Action film", genres="Action, Drama")
        animation = await db.get_or_create_film("tt0000002", "Animation film", genres="Animation")
        self.assertNotEqual(action, animation)

        genres = await db.list_genres()
        self.assertEqual({item["name"] for item in genres}, {"Action", "Drama", "Animation"})
        items = await db.browse_by_genre(42, "Action")
        self.assertEqual([item["id"] for item in items], [action])

        await db.get_or_create_film("tt0000003", "Mystery film", genres="Mystery")
        refreshed = await db.list_genres()
        self.assertIn("Mystery", {item["name"] for item in refreshed})

