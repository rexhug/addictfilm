import tempfile
import unittest
from pathlib import Path

import database as db


class GenreStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": None})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def test_genre_stat_includes_exact_film_count_with_percentage(self):
        first = await db.get_or_create_film("tt0000011", "First", genres="Drama, Thriller")
        second = await db.get_or_create_film("tt0000012", "Second", genres="Drama")
        await db.set_status(1, first, "watched")
        await db.set_status(1, second, "watched")

        stats = await db.get_user_stats(1)

        self.assertEqual(stats["top_genres_pct"], [("Drama", 67, 2), ("Thriller", 33, 1)])
