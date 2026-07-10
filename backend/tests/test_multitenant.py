import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database as db


class MultiTenantDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.original_path = db.DB_PATH
        db.DB_PATH = self.path
        await db.init_db()
        self.first = await db.upsert_user({"id": 101, "first_name": "Аня"})
        self.second = await db.upsert_user({"id": 202, "first_name": "Борис"})

    async def asyncTearDown(self):
        db.DB_PATH = self.original_path
        os.unlink(self.path)

    async def _add_movie(self):
        return await db.get_or_create_movie(
            imdb_id="tt0000001",
            title="Тестовый фильм",
            year="2026",
            genres="драма, комедия",
            runtime="120 мин",
            imdb_rating="8.0",
            imdb_votes="100",
            plot=None,
            poster_url=None,
            directors="Режиссёр",
            actors="Актёр Один, Актёр Два",
        )

    async def test_lists_are_private_but_catalog_is_shared(self):
        movie_id = await self._add_movie()
        self.assertTrue(await db.add_user_movie(self.first["id"], movie_id, "watched"))
        await db.set_rating(movie_id, self.first["id"], 9)

        first_movies = await db.get_movies_by_status(self.first["id"], "watched")
        second_movies = await db.get_movies_by_status(self.second["id"], "watched")

        self.assertEqual([movie["id"] for movie in first_movies], [movie_id])
        self.assertEqual(second_movies, [])
        self.assertIsNone(await db.get_movie(self.second["id"], movie_id))

        self.assertTrue(await db.add_user_movie(self.second["id"], movie_id, "watched"))
        await db.set_rating(movie_id, self.second["id"], 7)
        self.assertEqual(await db.count_movies(self.second["id"], "watched"), 1)

    async def test_partner_invite_creates_pair_and_common_statistics(self):
        movie_id = await self._add_movie()
        for user, rating in ((self.first, 8), (self.second, 6)):
            await db.add_user_movie(user["id"], movie_id, "watched")
            await db.set_rating(movie_id, user["id"], rating)

        invite = await db.create_partner_invite(
            self.first["id"], "token_for_partner_123", "2099-01-01T00:00:00+00:00",
        )
        result, partner = await db.accept_partner_invite(invite["token"], self.second["id"])
        pair = await db.get_pair_stats(self.first["id"], self.second["id"])

        self.assertEqual(result, "accepted")
        self.assertEqual(partner["id"], self.first["id"])
        self.assertEqual((await db.get_partner(self.first["id"]))["id"], self.second["id"])
        self.assertEqual(pair["shared_watched"], 1)
        self.assertEqual(pair["compatibility"], {"count": 1, "agreement": 78})
        self.assertIsNone(await db.create_partner_invite(
            self.first["id"], "second_partner_token_123", "2099-01-01T00:00:00+00:00",
        ))

    async def test_invite_cannot_be_accepted_by_its_author(self):
        invite = await db.create_partner_invite(
            self.first["id"], "self_invite_token_123", "2099-01-01T00:00:00+00:00",
        )
        result, partner = await db.accept_partner_invite(invite["token"], self.first["id"])

        self.assertEqual(result, "self")
        self.assertIsNone(partner)

    async def test_catalog_deduplicates_concurrent_requests(self):
        args = {
            "imdb_id": "tt0000002",
            "title": "Один каталог",
            "year": None,
            "genres": None,
            "runtime": None,
            "imdb_rating": None,
            "imdb_votes": None,
            "plot": None,
            "poster_url": None,
        }
        movie_ids = await asyncio.gather(*[db.get_or_create_movie(**args) for _ in range(6)])

        self.assertEqual(len(set(movie_ids)), 1)
