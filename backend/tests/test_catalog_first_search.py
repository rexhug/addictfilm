import asyncio
import tempfile
import unittest
from pathlib import Path

import cast as cast_model
import database as db
import db_runtime
import kinopoisk
import main
import search
from enrichment import cast_backfill
from fastapi import HTTPException


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

    async def test_imdb_and_kinopoisk_aliases_share_one_catalog_record(self):
        poster = "https://st.kp.yandex.net/brothers.jpg"
        synthetic_id = await db.get_or_create_film(
            "kp_253761", "Братья", year="2009", kp_id="253761",
            poster_url=poster,
        )

        # Simulates a temporary Kinopoisk-assets outage on the IMDb path: even
        # without kp_id the exact provider poster fingerprint is a safe bridge.
        imdb_id = await db.get_or_create_film(
            "tt0765010", "Братья", year="2009", poster_url=poster,
        )

        self.assertEqual(imdb_id, synthetic_id)
        film = await db.get_film(imdb_id)
        self.assertEqual(film["imdb_id"], "tt0765010")
        self.assertEqual(film["kp_id"], "253761")
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            count = await (await conn.execute(
                "SELECT COUNT(*) FROM films WHERE title = ? AND year = ?",
                ("Братья", "2009"),
            )).fetchone()
        self.assertEqual(count[0], 1)

    async def test_identity_migration_merges_latest_user_state_and_collection(self):
        await db.upsert_user({"id": 1, "first_name": "One"})
        await db.upsert_user({"id": 2, "first_name": "Two"})
        poster = "https://st.kp.yandex.net/brothers-migration.jpg"
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            canonical = await (await conn.execute(
                "INSERT INTO films (imdb_id,title,year,poster_url,search_text,created_at) "
                "VALUES (?,?,?,?,?,?) RETURNING id",
                ("tt0765010", "Братья", "2009", poster, "братья", "2026-01-01"),
            )).fetchone()
            duplicate = await (await conn.execute(
                "INSERT INTO films (imdb_id,kp_id,title,year,poster_url,search_text,created_at) "
                "VALUES (?,?,?,?,?,?,?) RETURNING id",
                ("kp_253761", "253761", "Братья", "2009", poster, "братья", "2026-02-01"),
            )).fetchone()
            canonical_id, duplicate_id = canonical[0], duplicate[0]
            await conn.execute(
                "INSERT INTO user_films "
                "(user_id,film_id,status,rating,added_at,watched_at,rated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (1, canonical_id, "watched", 9, "2026-01-01", "2026-01-02", "2026-01-03"),
            )
            await conn.execute(
                "INSERT INTO user_films "
                "(user_id,film_id,status,rating,added_at,watched_at,rated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (1, duplicate_id, "watched", 7, "2026-02-01", "2026-02-02", "2026-02-03"),
            )
            await conn.execute(
                "INSERT INTO user_films (user_id,film_id,status,added_at) VALUES (?,?,?,?)",
                (2, duplicate_id, "want_to_watch", "2026-02-01"),
            )
            collection = await (await conn.execute(
                "INSERT INTO collections (title,created_by,created_at,status) "
                "VALUES (?,?,?,?) RETURNING id",
                ("Test", 1, "2026-01-01", "published"),
            )).fetchone()
            collection_id = collection[0]
            await conn.execute(
                "INSERT INTO collection_films "
                "(collection_id,film_id,added_at,position,added_by) VALUES (?,?,?,?,?)",
                (collection_id, duplicate_id, "2026-02-01", 1, 1),
            )
            await conn.commit()

        await db._apply_film_identity_migration()

        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            conn.row_factory = db.aiosqlite.Row
            films = await (await conn.execute(
                "SELECT id,imdb_id,kp_id FROM films WHERE title = ?", ("Братья",)
            )).fetchall()
            states = await (await conn.execute(
                "SELECT user_id,status,rating FROM user_films WHERE film_id = ? ORDER BY user_id",
                (canonical_id,),
            )).fetchall()
            collection_film = await (await conn.execute(
                "SELECT film_id FROM collection_films WHERE collection_id = ?",
                (collection_id,),
            )).fetchone()
        self.assertEqual(
            [(row["id"], row["imdb_id"], row["kp_id"]) for row in films],
            [(canonical_id, "tt0765010", "253761")],
        )
        self.assertEqual(
            [(row["user_id"], row["status"], row["rating"]) for row in states],
            [(1, "watched", 7), (2, "want_to_watch", None)],
        )
        self.assertEqual(collection_film["film_id"], canonical_id)

    async def test_kinopoisk_assets_expose_cross_provider_id(self):
        old_tokens, old_request = kinopoisk.KINOPOISK_TOKENS, kinopoisk._request

        async def fake_request(_path, _params):
            return {
                "docs": [{
                    "id": 253761,
                    "externalId": {"imdb": "tt0765010"},
                    "poster": {"url": "https://st.kp.yandex.net/brothers.jpg"},
                }]
            }

        kinopoisk.KINOPOISK_TOKENS = ["test"]
        kinopoisk._request = fake_request
        try:
            assets = await kinopoisk.assets_by_imdb(["tt0765010"])
        finally:
            kinopoisk.KINOPOISK_TOKENS = old_tokens
            kinopoisk._request = old_request

        self.assertEqual(assets["tt0765010"]["kp_id"], "253761")

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

    async def test_direct_add_throttles_only_a_catalog_miss(self):
        old_allow, old_fetch = main.ratelimit.allow_user, main.search.fetch_details

        def denied(_user_id):
            return False

        async def must_not_fetch(_src, _ref):
            raise AssertionError("a throttled direct add must not contact providers")

        main.ratelimit.allow_user = denied
        main.search.fetch_details = must_not_fetch
        try:
            with self.assertRaises(HTTPException) as raised:
                await main.add(main.AddBody(src="i", ref="tt0133093"), {"id": 42})
        finally:
            main.ratelimit.allow_user = old_allow
            main.search.fetch_details = old_fetch
        self.assertEqual(raised.exception.status_code, 429)

    async def test_existing_catalog_row_is_enriched_without_overwriting_good_fields(self):
        film_id = await db.get_or_create_film(
            "tt0133093", "Матрица", actors="Киану Ривз", genres="Action", runtime="136 min",
        )
        same_id = await db.get_or_create_film(
            "tt0133093", "The Matrix", title_original="The Matrix", year="1999",
            genres="Action, Sci-Fi", directors="Лана Вачовски, Лилли Вачовски",
            actors="Киану Ривз, Лоренс Фишберн, Кэрри-Энн Мосс", runtime="136 min",
            plot="A hacker learns the truth.", kp_id="301",
        )
        stored = await db.get_film(film_id)
        self.assertEqual(same_id, film_id)
        self.assertEqual(stored["title"], "Матрица")
        self.assertEqual(stored["year"], "1999")
        self.assertEqual(stored["genres"], "Action, Sci-Fi")
        self.assertEqual(stored["directors"], "Лана Вачовски, Лилли Вачовски")
        self.assertIn("Лоренс Фишберн", stored["actors"])
        self.assertIn("лоренс фишберн", stored["search_text"])
        self.assertEqual((await db.browse_by_genre(42, "Sci-Fi"))[0]["id"], film_id)

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
        old_kp_resolve = main.kinopoisk.cast_documents_by_imdb
        old_store = main.db.update_film_cast
        # Фоновое обогащение проверяет портреты по сети. Здесь измеряется не
        # доступность Commons, а то, что запрос не ждёт обогащения, поэтому
        # проверка портрета подменяется: иначе тест зависит от чужого сервиса и
        # от его скорости ответа.
        old_probe = cast_backfill.probe_portrait

        async def offline_probe(url, *, session):
            return cast_model.PortraitProbe("unknown")

        async def researched_cast(ids, max_actors=10):
            self.assertEqual(ids, ["tt0765010"])
            self.assertEqual(max_actors, 12)
            started.set()
            await release.wait()
            return {"tt0765010": [{"name": "Тоби Магуайр", "photo_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Tobey.jpg"}]}

        async def store_cast(*args, **kwargs):
            result = await old_store(*args, **kwargs)
            persisted.set()
            return result

        async def no_kinopoisk_match(ids):
            self.assertEqual(ids, ["tt0765010"])
            return {}

        main.wikidata.get_cast_by_imdb = researched_cast
        main.kinopoisk.cast_documents_by_imdb = no_kinopoisk_match
        main.db.update_film_cast = store_cast
        cast_backfill.probe_portrait = offline_probe
        try:
            response = await asyncio.wait_for(main.movie(film_id, {"id": 1}), timeout=0.1)
            self.assertEqual(response["actors"], "Случайный актёр")
            await asyncio.wait_for(started.wait(), timeout=0.1)
            release.set()
            await asyncio.wait_for(persisted.wait(), timeout=0.5)
        finally:
            main.wikidata.get_cast_by_imdb = old_cast
            main.kinopoisk.cast_documents_by_imdb = old_kp_resolve
            main.db.update_film_cast = old_store
            cast_backfill.probe_portrait = old_probe

        stored = await db.get_film(film_id)
        self.assertEqual(stored["actors"], "Тоби Магуайр")
        self.assertEqual(db.decode_film_cast(stored["cast_json"])[0]["name"], "Тоби Магуайр")
        self.assertIsNotNone(stored["actor_photos_checked_at"])
