import tempfile
import unittest
from pathlib import Path

import database as db


class GenreTaxonomyTests(unittest.IsolatedAsyncioTestCase):
    """Кураторская витрина жанров: языковые/смысловые дубли мёржатся в русский
    канон, форматы и мусор в витрину не попадают, browse матчит все алиасы."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        db._invalidate_genres_cache()
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": None})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        db._invalidate_genres_cache()
        self.temp_dir.cleanup()

    async def _genres(self):
        db._invalidate_genres_cache()  # кэш переживает setUp — сбрасываем явно
        return await db.list_genres()

    async def test_language_duplicates_merge_into_one_russian_canon(self):
        await db.get_or_create_film("tt0000001", "A", genres="Drama")
        await db.get_or_create_film("tt0000002", "B", genres="драма")
        items = await self._genres()
        names = [item["name"] for item in items]
        self.assertEqual(names.count("драма"), 1)
        self.assertEqual(next(i["count"] for i in items if i["name"] == "драма"), 2)
        self.assertNotIn("Drama", names)

    async def test_semantic_duplicates_merge(self):
        # аниме/Animation → мультфильм; детский/Kids → семейный; вестерн → приключения
        await db.get_or_create_film("tt0000003", "C", genres="аниме")
        await db.get_or_create_film("tt0000004", "D", genres="Animation")
        await db.get_or_create_film("tt0000005", "E", genres="мультфильм")
        await db.get_or_create_film("tt0000006", "F", genres="детский")
        await db.get_or_create_film("tt0000007", "G", genres="Family")
        await db.get_or_create_film("tt0000008", "H", genres="вестерн")
        items = await self._genres()
        by = {i["name"]: i["count"] for i in items}
        self.assertEqual(by.get("мультфильм"), 3)
        self.assertEqual(by.get("семейный"), 2)
        self.assertEqual(by.get("приключения"), 1)
        for stray in ("аниме", "детский", "вестерн", "Animation", "Family"):
            self.assertNotIn(stray, by)

    async def test_formats_and_noise_are_excluded_from_showcase(self):
        await db.get_or_create_film("tt0000009", "I", genres="короткометражка")
        await db.get_or_create_film("tt0000010", "J", genres="реальное ТВ")
        await db.get_or_create_film("tt0000011", "K", genres="спорт")
        await db.get_or_create_film("tt0000012", "L", genres="Drama")
        names = [i["name"] for i in await self._genres()]
        self.assertEqual(names, ["драма"])

    async def test_sorted_by_count_descending(self):
        for n in range(3):
            await db.get_or_create_film(f"tt000010{n}", f"D{n}", genres="Thriller")
        await db.get_or_create_film("tt0000201", "X", genres="ужасы")
        names = [i["name"] for i in await self._genres()]
        self.assertEqual(names, ["триллер", "ужасы"])

    async def test_browse_matches_all_aliases_of_canonical_genre(self):
        anime = await db.get_or_create_film("tt0000301", "Anime", genres="аниме")
        cartoon = await db.get_or_create_film("tt0000302", "Cartoon", genres="Animation")
        toon = await db.get_or_create_film("tt0000303", "Toon", genres="мультфильм")
        kid = await db.get_or_create_film("tt0000304", "Kid", genres="детский")
        items = await db.browse_by_genre(1, "мультфильм", limit=10)
        ids = {i["id"] for i in items}
        self.assertEqual(ids, {anime, cartoon, toon})
        family = {i["id"] for i in await db.browse_by_genre(1, "семейный", limit=10)}
        self.assertEqual(family, {kid})


if __name__ == "__main__":
    unittest.main()
