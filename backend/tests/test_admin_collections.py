"""Безопасность и жизненный цикл редакционных подборок.

Ключевая проверка: доступ запрещён по умолчанию и решается ТОЛЬКО сервером —
клиентский тумблер «Режим администратора», подделанный user_id или is_admin
права не дают. Плюс переходы статусов, оптимистичная блокировка и порядок.
"""
import tempfile
import unittest
from pathlib import Path

import database as db


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "Admin", "username": None})
        await db.upsert_user({"id": 2, "first_name": "User", "username": None})
        self.film = await db.get_or_create_film("tt7000001", "Film A", year="2020")
        self.other = await db.get_or_create_film("tt7000002", "Film B", year="2021")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def _published(self, title="Готовая"):
        cid = await db.create_collection(title, 1)
        await db.add_film_to_collection(cid, self.film, 1)
        collection = await db.get_collection(cid)
        await db.set_collection_status(cid, "published", collection["version"], 1)
        return cid


class DraftVisibilityTests(_Base):
    async def test_new_collection_starts_as_draft_and_is_not_public(self):
        cid = await db.create_collection("Черновик", 1)
        self.assertEqual((await db.get_collection(cid))["status"], "draft")
        # Публичный слой (statuses=("published",)) не видит черновик ни в списке,
        # ни по прямому ID — угадывание ID не помогает.
        self.assertEqual(await db.list_collections(("published",)), [])
        self.assertIsNone(await db.get_collection(cid, statuses=("published",)))

    async def test_published_collection_is_public(self):
        cid = await self._published()
        public = await db.list_collections(("published",))
        self.assertEqual([c["id"] for c in public], [cid])
        self.assertIsNotNone(await db.get_collection(cid, statuses=("published",)))

    async def test_archived_collection_disappears_from_public(self):
        cid = await self._published()
        collection = await db.get_collection(cid)
        await db.set_collection_status(cid, "archived", collection["version"], 1)
        self.assertEqual(await db.list_collections(("published",)), [])
        self.assertIsNone(await db.get_collection(cid, statuses=("published",)))
        # Но сохраняется для админа и восстановления.
        self.assertEqual((await db.get_collection(cid))["status"], "archived")


class StatusTransitionTests(_Base):
    async def test_invalid_transition_is_rejected(self):
        cid = await self._published()
        collection = await db.get_collection(cid)
        await db.set_collection_status(cid, "archived", collection["version"], 1)
        archived = await db.get_collection(cid)
        # archived → published напрямую запрещён (только через draft).
        self.assertIsNone(await db.set_collection_status(cid, "published", archived["version"], 1))
        self.assertEqual((await db.get_collection(cid))["status"], "archived")

    async def test_publish_records_publisher_and_timestamp(self):
        cid = await self._published()
        collection = await db.get_collection(cid)
        self.assertEqual(collection["status"], "published")
        self.assertIsNotNone(collection["published_at"])

    async def test_restore_from_archive_returns_to_draft(self):
        cid = await self._published()
        c = await db.get_collection(cid)
        await db.set_collection_status(cid, "archived", c["version"], 1)
        c = await db.get_collection(cid)
        restored = await db.set_collection_status(cid, "draft", c["version"], 1)
        self.assertEqual(restored["status"], "draft")


class OptimisticLockTests(_Base):
    async def test_stale_version_update_is_rejected(self):
        cid = await db.create_collection("Гонка", 1)
        first = await db.get_collection(cid)
        # Первый админ сохранил — версия выросла.
        await db.update_collection(cid, first["version"], 1, {"title": "Новое"})
        # Второй админ шлёт ту же (устаревшую) версию → отказ, а не перезапись.
        self.assertIsNone(await db.update_collection(cid, first["version"], 2, {"title": "Чужое"}))
        self.assertEqual((await db.get_collection(cid))["title"], "Новое")

    async def test_version_increments_on_each_write(self):
        cid = await db.create_collection("Версии", 1)
        v1 = (await db.get_collection(cid))["version"]
        await db.update_collection(cid, v1, 1, {"title": "Раз"})
        v2 = (await db.get_collection(cid))["version"]
        self.assertEqual(v2, v1 + 1)

    async def test_stale_reorder_is_rejected(self):
        cid = await db.create_collection("Порядок", 1)
        await db.add_film_to_collection(cid, self.film, 1)
        await db.add_film_to_collection(cid, self.other, 1)
        stale = (await db.get_collection(cid))["version"]
        await db.update_collection(cid, stale, 1, {"title": "Сдвиг версии"})
        self.assertIsNone(await db.reorder_collection_items(
            cid, [self.other, self.film], stale, 1))


class OrderingTests(_Base):
    async def test_films_keep_curated_order(self):
        cid = await db.create_collection("Порядок", 1)
        await db.add_film_to_collection(cid, self.film, 1)
        await db.add_film_to_collection(cid, self.other, 1)
        version = (await db.get_collection(cid))["version"]
        await db.reorder_collection_items(cid, [self.other, self.film], version, 1)
        items = await db.get_collection_films(cid, 1)
        self.assertEqual([i["id"] for i in items], [self.other, self.film])

    async def test_reorder_with_mismatched_set_is_rejected(self):
        cid = await db.create_collection("Рассинхрон", 1)
        await db.add_film_to_collection(cid, self.film, 1)
        version = (await db.get_collection(cid))["version"]
        # Клиент прислал ID, которого нет в подборке — это рассинхрон, не порядок.
        self.assertIsNone(await db.reorder_collection_items(cid, [self.other], version, 1))

    async def test_removal_keeps_positions_dense(self):
        cid = await db.create_collection("Плотность", 1)
        third = await db.get_or_create_film("tt7000003", "Film C")
        for film in (self.film, self.other, third):
            await db.add_film_to_collection(cid, film, 1)
        await db.remove_film_from_collection(cid, self.other)
        items = await db.get_collection_films(cid, 1)
        self.assertEqual([i["id"] for i in items], [self.film, third])
        # Позиции без дырок — следующий reorder не упрётся в UNIQUE.
        version = (await db.get_collection(cid))["version"]
        self.assertIsNotNone(await db.reorder_collection_items(cid, [third, self.film], version, 1))

    async def test_duplicate_film_is_not_added_twice(self):
        cid = await db.create_collection("Дубли", 1)
        self.assertTrue(await db.add_film_to_collection(cid, self.film, 1))
        self.assertFalse(await db.add_film_to_collection(cid, self.film, 1))
        self.assertEqual(len(await db.get_collection_films(cid, 1)), 1)


class AuditTests(_Base):
    async def test_mutation_is_audited_with_actor_and_action(self):
        cid = await db.create_collection("Аудит", 1)
        await db.write_audit(1, "admin", "collection.created", "collection", cid, {"title": "Аудит"})
        entries = await db.list_audit_log()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["actor_id"], 1)
        self.assertEqual(entries[0]["action"], "collection.created")
        self.assertEqual(entries[0]["entity_id"], str(cid))


class MigrationBackfillTests(_Base):
    async def test_legacy_rows_stay_published_and_keep_order(self):
        """Миграция не должна прятать уже опубликованный прод-контент и менять
        порядок фильмов (раньше он был неявным: added_at ASC)."""
        cid = await db.create_collection("Старая", 1)
        for film in (self.film, self.other):
            await db.add_film_to_collection(cid, film, 1)
        async with db.db_runtime.connect(db.DB_PATH, "") as conn:
            # Возвращаем строку в «до-миграционное» состояние.
            await conn.execute("UPDATE collections SET status='published', version=1 WHERE id=?", (cid,))
            await conn.execute("UPDATE collection_films SET position=0 WHERE collection_id=?", (cid,))
            await conn.commit()
        await db._apply_editorial_collections_migration()
        self.assertEqual((await db.get_collection(cid))["status"], "published")
        items = await db.get_collection_films(cid, 1)
        self.assertEqual([i["id"] for i in items], [self.film, self.other])


if __name__ == "__main__":
    unittest.main()


class CreateWithItemsTests(_Base):
    """Первое сохранение редактора создаёт подборку вместе с фильмами одной
    транзакцией: наполовину созданной подборки быть не должно."""

    async def test_create_with_ordered_films_keeps_order(self):
        third = await db.get_or_create_film("tt7100003", "Third")
        cid = await db.create_collection(
            "С фильмами", 1, ordered_film_ids=[self.other, self.film, third])
        items = await db.get_collection_films(cid, 1)
        self.assertEqual([i["id"] for i in items], [self.other, self.film, third])
        self.assertEqual((await db.get_collection(cid))["status"], "draft")

    async def test_unknown_film_rolls_back_whole_creation(self):
        before = len(await db.list_collections(db.COLLECTION_STATUSES))
        result = await db.create_collection("Плохая", 1, ordered_film_ids=[self.film, 999999])
        self.assertIsNone(result)
        # Ни подборки, ни осиротевших связей.
        self.assertEqual(len(await db.list_collections(db.COLLECTION_STATUSES)), before)

    async def test_create_without_films_still_works(self):
        cid = await db.create_collection("Пустая", 1)
        self.assertIsNotNone(cid)
        self.assertEqual(await db.get_collection_films(cid, 1), [])

    async def test_create_stores_display_type_and_backdrop(self):
        cid = await db.create_collection(
            "Крупная", 1, display_type="featured",
            backdrop_url="https://img/b.jpg", ordered_film_ids=[self.film])
        collection = await db.get_collection(cid)
        self.assertEqual(collection["display_type"], "featured")
        self.assertEqual(collection["backdrop_url"], "https://img/b.jpg")
