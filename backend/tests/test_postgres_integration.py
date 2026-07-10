"""Проверка PostgreSQL запускается только при явно заданном TEST_DATABASE_URL."""
import asyncio
import os
import sys
import unittest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database as db
import db_runtime


@unittest.skipUnless(
    db_runtime.uses_postgres(TEST_DATABASE_URL),
    "Для PostgreSQL-интеграции задайте TEST_DATABASE_URL",
)
class PostgresIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.init_db()
        async with db._connect() as connection:
            await connection.execute(
                "TRUNCATE partner_invites, partnerships, comments, ratings, user_movies, users, movies "
                "RESTART IDENTITY CASCADE"
            )
        self.first = await db.upsert_user({"id": 1_001, "first_name": "Аня"})
        self.second = await db.upsert_user({"id": 2_002, "first_name": "Борис"})

    async def asyncTearDown(self):
        await db.close_db()

    async def test_catalog_pair_and_statistics_work_in_postgres(self):
        data = {
            "imdb_id": "tt0000009",
            "title": "Проверка PostgreSQL",
            "year": "2026",
            "genres": "драма",
            "runtime": "100 мин",
            "imdb_rating": None,
            "imdb_votes": None,
            "plot": None,
            "poster_url": None,
        }
        movie_ids = await asyncio.gather(*[db.get_or_create_movie(**data) for _ in range(5)])
        self.assertEqual(len(set(movie_ids)), 1)
        movie_id = movie_ids[0]

        for user, rating in ((self.first, 9), (self.second, 7)):
            self.assertTrue(await db.add_user_movie(user["id"], movie_id, "watched"))
            await db.set_rating(movie_id, user["id"], rating)

        invite = await db.create_partner_invite(
            self.first["id"], "postgres_integration_token_123", "2099-01-01T00:00:00+00:00",
        )
        result, partner = await db.accept_partner_invite(invite["token"], self.second["id"])
        stats = await db.get_pair_stats(self.first["id"], self.second["id"])

        self.assertEqual(result, "accepted")
        self.assertEqual(partner["id"], self.first["id"])
        self.assertEqual(stats["shared_watched"], 1)
        self.assertEqual(stats["compatibility"], {"count": 1, "agreement": 78})
