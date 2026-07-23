import asyncio
import tempfile
import unittest
from pathlib import Path

import database as db
import kinopoisk
import main
import search


class CatalogFirstSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        search._QCACHE.clear()
        await db.init_db()

    async def asyncTearDown(self):
        search._QCACHE.clear()
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def test_catalog_is_searched_before_kinopoisk_with_unicode_casefolding(self):
        await db.get_or_create_film(
            "tt0133093", "Матрица", title_original="The Matrix", actors="Киану Ривз",
            kp_id="301", poster_url="https://st.kp.yandex.net/matrix.jpg")

        old_search = search.kinopoisk.search_movies

        async def external_call_is_a_failure(_query):
            raise AssertionError("catalog hit must not call Kinopoisk")

        search.kinopoisk.search_movies = external_call_is_a_failure
        try:
            result = await search.cached_search("МАТРИЦА", user_id=1)
        finally:
            search.kinopoisk.search_movies = old_search

        self.assertTrue(result["cached"])
        self.assertEqual(result["items"][0]["ref"], "tt0133093")
        self.assertEqual(result["items"][0]["src"], "i")

    async def test_kinopoisk_search_response_becomes_a_permanent_catalog_entry(self):
        document = {
            "id": 301,
            "externalId": {"imdb": "tt0133093"},
            "name": "Матрица",
            "alternativeName": "The Matrix",
            "year": 1999,
            "genres": [{"name": "фантастика"}],
            "rating": {"kp": 8.5, "imdb": 8.7},
            "votes": {"imdb": 2_000_000},
            "poster": {"url": "https://st.kp.yandex.net/matrix.jpg"},
            "persons": [],
        }
        old_token, old_search = search.KINOPOISK_TOKEN, search.kinopoisk.search_movies

        async def fake_search(_query):
            return [document]

        search.KINOPOISK_TOKEN = "test-token"
        search.kinopoisk.search_movies = fake_search
        try:
            items = await search.find_movies("матрица")
        finally:
            search.KINOPOISK_TOKEN = old_token
            search.kinopoisk.search_movies = old_search

        self.assertEqual(items[0]["ref"], "301")
        self.assertIsNotNone(await db.get_film_id_by_source("k", "301"))
        self.assertEqual((await db.search_catalog("the matrix"))[0]["ref"], "tt0133093")

    async def test_budget_gate_stops_http_before_connecting_to_kinopoisk(self):
        old_keys, old_spend = kinopoisk.KINOPOISK_TOKENS, kinopoisk.ratelimit.try_spend_search

        async def no_budget():
            return False

        kinopoisk.KINOPOISK_TOKENS = ["test-token"]
        kinopoisk.ratelimit.try_spend_search = no_budget
        try:
            self.assertIsNone(await kinopoisk._request("/movie/301", []))
        finally:
            kinopoisk.KINOPOISK_TOKENS = old_keys
            kinopoisk.ratelimit.try_spend_search = old_spend

    async def test_direct_imdb_lookup_is_saved_and_returns_a_selectable_item(self):
        old_fetch = main.search.fetch_details

        async def fake_details(src, ref):
            self.assertEqual((src, ref), ("i", "tt0133093"))
            return {
                "imdb_id": ref,
                "title": "Матрица",
                "title_original": "The Matrix",
                "year": "1999",
                "poster_url": "https://st.kp.yandex.net/matrix.jpg",
            }

        main.search.fetch_details = fake_details
        try:
            result = await main.api_search("tt0133093", {"id": 987654})
        finally:
            main.search.fetch_details = old_fetch

        self.assertEqual(result["items"][0]["src"], "i")
        self.assertEqual(result["items"][0]["ref"], "tt0133093")
        self.assertIsNotNone(await db.get_film_id_by_source("i", "tt0133093"))

    async def test_omdb_fallback_result_also_becomes_a_permanent_catalog_entry(self):
        record = {
            "imdbID": "tt0133093", "Title": "The Matrix", "Year": "1999",
            "Poster": "https://images.example/matrix.jpg", "Type": "movie",
            "Genre": "Action, Sci-Fi", "Director": "Lana Wachowski",
            "Actors": "Keanu Reeves", "Runtime": "136 min", "imdbRating": "8.7",
            "imdbVotes": "2000000", "Plot": "A hacker learns the truth.",
        }
        old_token = search.KINOPOISK_TOKEN
        old_search = search.omdb.search_movies
        old_movie = search.omdb.get_movie
        old_titles = search.wikidata.get_titles_by_imdb

        async def fake_search(_query):
            return [record], False, False

        async def fake_movie(_imdb_id):
            return record

        async def fake_titles(_ids, _language):
            return {"tt0133093": "Матрица"}

        search.KINOPOISK_TOKEN = ""
        search.omdb.search_movies = fake_search
        search.omdb.get_movie = fake_movie
        search.wikidata.get_titles_by_imdb = fake_titles
        try:
            await search.find_movies("матрица")
        finally:
            search.KINOPOISK_TOKEN = old_token
            search.omdb.search_movies = old_search
            search.omdb.get_movie = old_movie
            search.wikidata.get_titles_by_imdb = old_titles

        self.assertEqual((await db.search_catalog("матрица"))[0]["ref"], "tt0133093")

    async def test_missing_visual_assets_are_checked_once_and_do_not_retry_forever(self):
        film_id = await db.get_or_create_film("tt0133093", "Матрица")
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()
        persisted = asyncio.Event()
        old_assets = main.kinopoisk.assets_by_imdb
        old_mark_visuals = main.db.mark_film_visuals_checked

        async def no_visual_assets(ids):
            nonlocal calls
            calls += 1
            self.assertEqual(ids, ["tt0133093"])
            started.set()
            await release.wait()
            return {"tt0133093": {"poster_url": None, "backdrop_url": None, "age_rating": None}}

        async def mark_visuals(*args, **kwargs):
            result = await old_mark_visuals(*args, **kwargs)
            persisted.set()
            return result

        main.kinopoisk.assets_by_imdb = no_visual_assets
        main.db.mark_film_visuals_checked = mark_visuals
        try:
            # The page response must not wait for a slow provider request.
            response = await asyncio.wait_for(main.movie(film_id, {"id": 1}), timeout=0.1)
            self.assertIsNone(response["poster_url"])
            await asyncio.wait_for(started.wait(), timeout=0.1)
            await main.movie(film_id, {"id": 1})
            release.set()
            await asyncio.wait_for(persisted.wait(), timeout=0.5)
        finally:
            main.kinopoisk.assets_by_imdb = old_assets
            main.db.mark_film_visuals_checked = old_mark_visuals

        stored = await db.get_film(film_id)
        self.assertEqual(calls, 1)
        self.assertIsNotNone(stored["poster_checked_at"])
        self.assertIsNotNone(stored["artwork_checked_at"])

    async def test_people_enrichment_is_backgrounded_and_persists_a_better_cast(self):
        film_id = await db.get_or_create_film(
            "tt0765010", "Братья", actors="Случайный актёр",
            poster_url="https://st.kp.yandex.net/poster.jpg", backdrop_url="https://st.kp.yandex.net/backdrop.jpg",
        )
        started = asyncio.Event()
        release = asyncio.Event()
        persisted = asyncio.Event()
        old_cast = main.wikidata.get_cast_by_imdb
        old_store = main.db.set_film_cast_from_wikidata

        async def researched_cast(ids):
            self.assertEqual(ids, ["tt0765010"])
            started.set()
            await release.wait()
            return {"tt0765010": [{"name": "Тоби Магуайр", "photo_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Tobey.jpg"}]}

        async def store_cast(*args, **kwargs):
            result = await old_store(*args, **kwargs)
            persisted.set()
            return result

        main.wikidata.get_cast_by_imdb = researched_cast
        main.db.set_film_cast_from_wikidata = store_cast
        try:
            response = await asyncio.wait_for(main.movie(film_id, {"id": 1}), timeout=0.1)
            self.assertEqual(response["actors"], "Случайный актёр")
            await asyncio.wait_for(started.wait(), timeout=0.1)
            release.set()
            await asyncio.wait_for(persisted.wait(), timeout=0.5)
        finally:
            main.wikidata.get_cast_by_imdb = old_cast
            main.db.set_film_cast_from_wikidata = old_store

        stored = await db.get_film(film_id)
        self.assertEqual(stored["actors"], "Тоби Магуайр")
        self.assertIsNotNone(stored["actor_photos_checked_at"])
