import tempfile
import unittest
from pathlib import Path

import database as db
import db_runtime


class WatchedSortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": None})
        # Год фильма НАМЕРЕННО обратен времени просмотра, чтобы поймать сортировку
        # по release year вместо watched_at.
        #   ref, year, rating, watched_at
        self.rows = [
            ("A", "2024", 10, "2026-01-01T00:00:00+00:00"),
            ("B", "2023", 7,  "2026-02-01T00:00:00+00:00"),
            ("C", "2022", 4,  "2026-03-01T00:00:00+00:00"),
            ("D", "2021", 7,  "2026-04-01T00:00:00+00:00"),  # ничья по рейтингу с B
            ("E", "2020", None, "2026-05-01T00:00:00+00:00"),  # без оценки
        ]
        self.film_ids = {}
        for ref, year, rating, watched_at in self.rows:
            fid = await db.get_or_create_film(f"tt{ord(ref):07d}", ref, year=year, genres="Drama")
            self.film_ids[ref] = fid
            async with db_runtime.connect(db.DB_PATH, "") as conn:
                await conn.execute(
                    "INSERT INTO user_films (user_id, film_id, status, rating, added_at, watched_at) "
                    "VALUES (1, ?, 'watched', ?, ?, ?)", (fid, rating, watched_at, watched_at))
                await conn.commit()

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def _order(self, sort):
        items = await db.get_user_films(1, "watched", limit=50, sort=sort)
        # вернём буквенные ref по порядку (title == ref)
        return [it["title"] for it in items]

    async def test_best_rating_desc_unrated_last(self):
        order = await self._order("best")
        # 10, затем ничья 7/7 (стабильно по watched_at DESC → D перед B), 4, потом без оценки E
        self.assertEqual(order, ["A", "D", "B", "C", "E"])

    async def test_worst_rating_asc_unrated_last(self):
        order = await self._order("worst")
        self.assertEqual(order, ["C", "D", "B", "A", "E"])

    async def test_new_first_by_watched_at(self):
        self.assertEqual(await self._order("new"), ["E", "D", "C", "B", "A"])

    async def test_old_first_by_watched_at_not_year(self):
        # A самый новый по ГОДУ (2024), но просмотрен раньше всех → при «старые
        # сначала» он ПЕРВЫЙ. Это доказывает сортировку по watched_at, а не по году.
        self.assertEqual(await self._order("old"), ["A", "B", "C", "D", "E"])

    async def test_equal_ratings_are_stable(self):
        first = await self._order("best")
        second = await self._order("best")
        self.assertEqual(first, second)  # детерминированный вторичный ключ

    async def test_unknown_sort_falls_back_to_newest(self):
        self.assertEqual(await self._order("bogus"), await self._order("new"))


if __name__ == "__main__":
    unittest.main()
