"""Крупные (featured) подборки: формат подачи, картинка и её fallback.

display_type отвечает ТОЛЬКО за представление: он не даёт прав, не публикует
черновик и не меняет состав фильмов при переключении.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import database as db
import main


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "Admin", "username": None})
        self.film = await db.get_or_create_film(
            "tt8000001", "With art", year="2020",
            poster_url="https://img/poster.jpg", backdrop_url="https://img/backdrop.jpg")
        self.bare = await db.get_or_create_film("tt8000002", "No art", year="2021")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def _collection(self, film=None, featured=False, publish=False):
        cid = await db.create_collection("Подборка", 1)
        await db.add_film_to_collection(cid, self.film if film is None else film, 1)
        if featured:
            version = (await db.get_collection(cid))["version"]
            await db.update_collection(cid, version, 1, {"display_type": "featured"})
        if publish:
            version = (await db.get_collection(cid))["version"]
            await db.set_collection_status(cid, "published", version, 1)
        return cid


class MigrationDefaultTests(_Base):
    async def test_existing_collections_default_to_standard(self):
        cid = await db.create_collection("Старая", 1)
        self.assertEqual((await db.get_collection(cid))["display_type"], "standard")

    async def test_migration_is_idempotent(self):
        cid = await self._collection(featured=True)
        await db._apply_featured_collections_migration()
        # Повторный прогон не сбрасывает уже выбранный формат.
        self.assertEqual((await db.get_collection(cid))["display_type"], "featured")


class DisplayTypeTests(_Base):
    async def test_conversion_preserves_id_films_and_status(self):
        cid = await self._collection(publish=True)
        before = await db.get_collection(cid)
        films_before = [i["id"] for i in await db.get_collection_films(cid, 1)]

        await db.update_collection(cid, before["version"], 1, {"display_type": "featured"})
        after = await db.get_collection(cid)

        self.assertEqual(after["id"], cid)
        self.assertEqual(after["status"], before["status"])  # публикация не тронута
        self.assertEqual([i["id"] for i in await db.get_collection_films(cid, 1)], films_before)
        self.assertEqual(after["display_type"], "featured")

    async def test_backdrop_survives_switch_back_to_standard(self):
        cid = await self._collection(featured=True)
        version = (await db.get_collection(cid))["version"]
        await db.update_collection(cid, version, 1, {"backdrop_url": "https://img/custom.jpg"})
        version = (await db.get_collection(cid))["version"]
        await db.update_collection(cid, version, 1, {"display_type": "standard"})
        # Фон сохранён: повторное переключение не заставит искать картинку заново.
        self.assertEqual((await db.get_collection(cid))["backdrop_url"], "https://img/custom.jpg")

    async def test_invalid_display_type_is_rejected_by_api(self):
        cid = await self._collection()
        body = main.CollectionPatchBody(version=1, display_type="banner")
        with patch.object(main, "_audit"), self.assertRaises(HTTPException) as ctx:
            await main.collection_update(cid, body, user={"id": 1})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["code"], "INVALID_DISPLAY_TYPE")


class BackdropFallbackTests(_Base):
    async def test_explicit_backdrop_wins(self):
        cid = await self._collection()
        version = (await db.get_collection(cid))["version"]
        await db.update_collection(cid, version, 1, {"backdrop_url": "https://img/explicit.jpg"})
        self.assertEqual((await db.get_collection(cid))["backdrop"], "https://img/explicit.jpg")

    async def test_falls_back_to_film_backdrop(self):
        cid = await self._collection()
        self.assertEqual((await db.get_collection(cid))["backdrop"], "https://img/backdrop.jpg")

    async def test_falls_back_to_cover_then_poster(self):
        cid = await self._collection(film=self.bare)
        version = (await db.get_collection(cid))["version"]
        await db.update_collection(cid, version, 1, {"cover_url": "https://img/cover.jpg"})
        self.assertEqual((await db.get_collection(cid))["backdrop"], "https://img/cover.jpg")

        other = await db.create_collection("Только постер", 1)
        await db.add_film_to_collection(other, self.film, 1)
        async with db.db_runtime.connect(db.DB_PATH, "") as conn:
            await conn.execute("UPDATE films SET backdrop_url = NULL WHERE id = ?", (self.film,))
            await conn.commit()
        self.assertEqual((await db.get_collection(other))["backdrop"], "https://img/poster.jpg")

    async def test_collection_without_any_art_has_no_backdrop(self):
        cid = await self._collection(film=self.bare)
        self.assertIsNone((await db.get_collection(cid))["backdrop"])


class PublicVisibilityTests(_Base):
    async def test_featured_draft_is_not_public(self):
        cid = await self._collection(featured=True)
        public = await db.list_collections(("published",))
        self.assertEqual(public, [])
        self.assertIsNone(await db.get_collection(cid, statuses=("published",)))

    async def test_published_featured_is_public_with_display_type(self):
        cid = await self._collection(featured=True, publish=True)
        public = await db.list_collections(("published",))
        self.assertEqual([c["id"] for c in public], [cid])
        self.assertEqual(public[0]["display_type"], "featured")
        self.assertIsNotNone(public[0]["backdrop"])

    async def test_archived_featured_disappears_publicly(self):
        cid = await self._collection(featured=True, publish=True)
        version = (await db.get_collection(cid))["version"]
        await db.set_collection_status(cid, "archived", version, 1)
        self.assertEqual(await db.list_collections(("published",)), [])

    async def test_collection_appears_in_exactly_one_format(self):
        featured = await self._collection(featured=True, publish=True)
        standard = await self._collection(publish=True)
        public = await db.list_collections(("published",))
        by_type = {}
        for item in public:
            by_type.setdefault(item["display_type"], []).append(item["id"])
        self.assertEqual(by_type["featured"], [featured])
        self.assertEqual(by_type["standard"], [standard])
        # Один и тот же ID не может оказаться в обеих группах.
        self.assertEqual(len(public), len({c["id"] for c in public}))


class SerializerTests(unittest.IsolatedAsyncioTestCase):
    def test_public_serializer_exposes_render_fields_only(self):
        row = {"id": 1, "title": "T", "description": "D", "cover": "c", "backdrop": "b",
               "display_type": "featured", "film_count": 3, "status": "published",
               "version": 4, "created_by": 9, "updated_at": "x", "published_at": "y",
               "backdrop_url": "raw", "sort_order": 0}
        public = main._public_collection(row)
        self.assertEqual(set(public), {"id", "title", "description", "cover", "backdrop",
                                       "display_type", "film_count"})
        for leaked in ("version", "created_by", "updated_at", "status", "published_at"):
            self.assertNotIn(leaked, public)


class ImageUrlValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_only_https_urls_are_accepted(self):
        self.assertTrue(main._safe_image_url("https://cdn.example/a.jpg"))
        for unsafe in ("http://cdn.example/a.jpg", "javascript:alert(1)",
                       "data:image/png;base64,AAA", "https://", "", "ftp://x/y.jpg"):
            with self.subTest(url=unsafe):
                self.assertFalse(main._safe_image_url(unsafe))


class FeaturedPublishGuardTests(_Base):
    async def test_featured_without_image_cannot_be_published(self):
        cid = await self._collection(film=self.bare, featured=True)
        with patch.object(main, "_audit"), self.assertRaises(HTTPException) as ctx:
            await main._transition(cid, "published", (await db.get_collection(cid))["version"],
                                   {"id": 1}, "collection.published")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["code"], "FEATURED_COLLECTION_IMAGE_REQUIRED")

    async def test_featured_with_resolvable_image_publishes(self):
        cid = await self._collection(featured=True)
        with patch.object(main, "_audit"):
            result = await main._transition(cid, "published",
                                            (await db.get_collection(cid))["version"],
                                            {"id": 1}, "collection.published")
        self.assertEqual(result["status"], "published")

    async def test_standard_without_image_still_publishes(self):
        cid = await self._collection(film=self.bare)
        with patch.object(main, "_audit"):
            result = await main._transition(cid, "published",
                                            (await db.get_collection(cid))["version"],
                                            {"id": 1}, "collection.published")
        self.assertEqual(result["status"], "published")


if __name__ == "__main__":
    unittest.main()
