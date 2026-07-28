"""Контракт API двух экранов подбора.

Режим изображения решает сервер. Клиент не должен ни знать про backdrop_url, ни
угадывать, годится ли кадр: ровно эта догадка и давала растянутый постер.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database as db
import main
import recommendations as rec

_HERO_FIELDS = ("hero_url", "hero_type", "hero_source", "hero_quality_score")


async def _keep_local_catalog(_user_id, _partner_id, _weights, candidates, _minimum):
    return candidates


class HeroApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "api.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})
        self.warm = mock.patch.object(rec, "_warm_catalog_if_sparse", new=_keep_local_catalog)
        self.warm.start()
        self.addCleanup(self.warm.stop)

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _film(self, imdb_id: str, *, poster="https://p/1.jpg") -> int:
        return await db.get_or_create_film(
            imdb_id=imdb_id, title=f"Фильм {imdb_id}", genres="драма", media_type="movie",
            poster_url=poster, year="2015", imdb_rating="7.4", imdb_votes="40000",
            plot="Обычное описание для подбора.")

    async def _wishlist_film(self, imdb_id="tt1", **kwargs) -> int:
        film_id = await self._film(imdb_id, **kwargs)
        await db.add_to_list(1, film_id, "want_to_watch", None)
        return film_id

    # ── рулетка по «Хочу» ────────────────────────────────────────────────────
    async def test_wishlist_random_returns_a_normalized_media_mode(self):
        film_id = await self._wishlist_film()
        await db.update_film_hero(film_id, hero_url="https://assets.fanart.tv/a.jpg",
                                  hero_type="backdrop", hero_source="fanart",
                                  hero_quality_score=0.91, hero_width=1920, hero_height=1080)
        payload = await main.wishlist_random(user={"id": 1})
        item = payload["item"]
        self.assertEqual(item["hero_type"], "backdrop")
        self.assertEqual(item["hero_source"], "fanart")
        self.assertEqual(item["hero_url"], "https://assets.fanart.tv/a.jpg")
        self.assertAlmostEqual(item["hero_quality_score"], 0.91)
        self.assertIn("cycle", payload)      # прежний контракт не тронут

    async def test_wishlist_random_falls_back_for_a_row_without_hero_columns(self):
        await self._wishlist_film()
        item = (await main.wishlist_random(user={"id": 1}))["item"]
        self.assertEqual(item["hero_type"], "poster_blur")
        self.assertEqual(item["hero_url"], "https://p/1.jpg")
        self.assertEqual(item["hero_source"], "poster")

    async def test_wishlist_random_declares_nothing_when_there_is_no_image_at_all(self):
        await self._wishlist_film("tt-none", poster=None)
        item = (await main.wishlist_random(user={"id": 1}))["item"]
        self.assertIsNone(item["hero_url"])
        self.assertIsNone(item["hero_type"])

    async def test_a_stored_kinopoisk_backdrop_does_not_become_a_hero(self):
        film_id = await self._wishlist_film()
        await db.set_film_artwork("tt1", "https://kp/wide.jpg", None)
        self.assertEqual((await db.get_film(film_id))["backdrop_url"], "https://kp/wide.jpg")
        item = (await main.wishlist_random(user={"id": 1}))["item"]
        self.assertEqual(item["hero_type"], "poster_blur")
        self.assertEqual(item["hero_url"], "https://p/1.jpg")

    # ── умный случайный ──────────────────────────────────────────────────────
    async def _seed_catalog(self) -> None:
        for index in range(14):
            await self._film(f"tt10{index:02d}")

    async def test_smart_random_returns_the_same_normalized_fields(self):
        await self._seed_catalog()
        body = main.RandomRecommendationBody(language="ru", context="solo")
        item = (await main.recommendation_random(body, user={"id": 1}))["item"]
        for field in _HERO_FIELDS:
            self.assertIn(field, item)
        self.assertEqual(item["hero_type"], "poster_blur")
        self.assertIn("strategy", item)      # прежний контракт не тронут
        self.assertIn("reasons", item)

    async def test_smart_random_uses_a_stored_backdrop_when_there_is_one(self):
        await self._seed_catalog()
        for film in await db.list_films_for_hero_backfill(limit=50):
            await db.update_film_hero(film["id"], hero_url="https://assets.fanart.tv/a.jpg",
                                      hero_type="backdrop", hero_source="fanart",
                                      hero_quality_score=0.88, hero_width=1920, hero_height=1080)
        body = main.RandomRecommendationBody(language="ru", context="solo")
        item = (await main.recommendation_random(body, user={"id": 1}))["item"]
        self.assertEqual(item["hero_type"], "backdrop")
        self.assertEqual(item["hero_source"], "fanart")

    # ── флаги и секреты ──────────────────────────────────────────────────────
    async def test_the_ui_flag_is_reported_by_the_server_not_guessed_by_the_client(self):
        with mock.patch.object(main, "FULLSCREEN_SINGLE_PICK_ENABLED", True):
            self.assertEqual(main._client_features(), {"fullscreen_single_pick": True})
        with mock.patch.object(main, "FULLSCREEN_SINGLE_PICK_ENABLED", False):
            self.assertEqual(main._client_features(), {"fullscreen_single_pick": False})

    async def test_bootstrap_carries_the_feature_state(self):
        payload = await main.me(user={"id": 1, "first_name": "One", "username": "one"})
        self.assertIn("fullscreen_single_pick", payload["features"])

    async def test_no_response_ever_carries_a_fanart_key(self):
        film_id = await self._wishlist_film()
        await db.update_film_hero(film_id, hero_url="https://assets.fanart.tv/a.jpg",
                                  hero_type="backdrop", hero_source="fanart",
                                  hero_quality_score=0.91, hero_width=1920, hero_height=1080)
        with mock.patch.object(main.fanart, "FANART_PROJECT_KEY", "supersecret"), \
             mock.patch.object(main.fanart, "FANART_CLIENT_KEY", "alsosecret"):
            payloads = [await main.wishlist_random(user={"id": 1}),
                        await main.me(user={"id": 1, "first_name": "One", "username": "one"}),
                        await main.admin_enrichment_status()]
        rendered = repr(payloads)
        self.assertNotIn("supersecret", rendered)
        self.assertNotIn("alsosecret", rendered)

    async def test_diagnostics_report_whether_a_key_exists_never_which(self):
        with mock.patch.object(main.fanart, "FANART_PROJECT_KEY", "supersecret"), \
             mock.patch.object(main.fanart, "FANART_CLIENT_KEY", ""):
            flags = (await main.admin_enrichment_status())["flags"]
        self.assertIs(flags["fanart_configured"], True)
        self.assertIn("fanart_hero_enabled", flags)

    async def test_the_fanart_cdn_is_the_only_host_added_to_the_image_proxy(self):
        self.assertIn("assets.fanart.tv", main._ALLOWED_IMG_HOSTS)
        self.assertTrue(main._is_allowed_image_url("https://assets.fanart.tv/fanart/movies/1/a.jpg"))
        self.assertFalse(main._is_allowed_image_url("https://webservice.fanart.tv/v3.2/movies/tt1"))
        self.assertFalse(main._is_allowed_image_url("https://evil.example/a.jpg"))


if __name__ == "__main__":
    unittest.main()
