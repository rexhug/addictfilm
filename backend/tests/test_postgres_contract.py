"""Contract tests for the production asyncpg adapter.

They skip locally unless TEST_DATABASE_URL is supplied. CI provides a disposable
PostgreSQL service, so SQLite-only tests cannot accidentally mask SQL-dialect or
transaction regressions in the production path.
"""
import os
import unittest

import database as db
import db_runtime


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(TEST_DATABASE_URL, "requires TEST_DATABASE_URL")
class PostgresContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        await db_runtime.close()
        db.DATABASE_URL = TEST_DATABASE_URL
        db._PG = True
        await db.init_db()
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "TRUNCATE TABLE collection_films, collections, partners, partner_invites, "
                "user_films, films, users, search_cache, search_budget RESTART IDENTITY CASCADE")
            await conn.commit()

    async def asyncTearDown(self):
        await db_runtime.close()
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg

    async def _add_users(self, *ids: int) -> None:
        for user_id in ids:
            await db.upsert_user({"id": user_id, "first_name": str(user_id), "username": None})

    async def test_invite_transaction_and_budget_use_postgres_placeholders(self):
        await self._add_users(1, 2)
        token = await db.create_invite(1)
        self.assertIsNotNone(token)

        result = await db.accept_invite(token, 2)

        self.assertEqual(result, {"ok": True, "partner_id": 1})
        self.assertEqual(await db.get_partner(1), 2)
        self.assertEqual(await db.get_partner(2), 1)
        self.assertIsNone(await db.create_invite(1))
        self.assertTrue(await db.try_spend_search_budget("2099-01-01", 1))
        self.assertFalse(await db.try_spend_search_budget("2099-01-01", 1))

    async def test_catalog_first_lookup_and_negative_artwork_cache_work_on_postgres(self):
        film_id = await db.get_or_create_film(
            "tt0133093", "Матрица", title_original="The Matrix",
            actors="Киану Ривз", kp_id="301",
        )

        self.assertEqual(await db.get_film_id_by_source("k", "301"), film_id)
        items = await db.search_catalog("МАТРИЦА")
        self.assertEqual(items[0]["ref"], "tt0133093")

        self.assertTrue(await db.mark_film_artwork_checked("tt0133093", None))
        film = await db.get_film(film_id)
        self.assertIsNotNone(film["artwork_checked_at"])

        self.assertTrue(await db.mark_film_visuals_checked("tt0133093", None, None))
        film = await db.get_film(film_id)
        self.assertIsNotNone(film["poster_checked_at"])
