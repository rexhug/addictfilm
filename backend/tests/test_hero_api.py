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
import deps
import main
import recommendations as rec

_HERO_FIELDS = (
    "hero_url", "hero_type", "hero_source", "hero_quality_score",
    "hero_fit", "hero_focus_x", "hero_focus_y",
)


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
            self.assertEqual(main._client_features(), {
                "fullscreen_single_pick": True,
                "cast_v2": False,
            })
        with mock.patch.object(main, "FULLSCREEN_SINGLE_PICK_ENABLED", False):
            self.assertEqual(main._client_features(), {
                "fullscreen_single_pick": False,
                "cast_v2": False,
            })

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

    async def test_no_external_image_call_happens_inside_a_user_request(self):
        """Человек нажимает кнопку и получает результат из каталога.

        Ни Fanart, ни скачивание файла на этом пути быть не должно: иначе
        рулетка начала бы зависеть от доступности чужого сервиса.
        """
        await self._wishlist_film()
        await self._seed_catalog()
        from enrichment import hero as hero_pipeline
        fanart_api = mock.AsyncMock(return_value=[])
        probe = mock.AsyncMock(return_value=hero_pipeline.ImageProbeResult("ok", 1920, 1080))
        with mock.patch.object(main.fanart, "get_movie_backgrounds", new=fanart_api), \
             mock.patch.object(hero_pipeline, "probe_image_info", new=probe):
            await main.wishlist_random(user={"id": 1})
            body = main.RandomRecommendationBody(language="ru", context="solo")
            await main.recommendation_random(body, user={"id": 1})
        fanart_api.assert_not_awaited()
        probe.assert_not_awaited()

    async def test_a_kinopoisk_backdrop_is_exposed_as_such(self):
        film_id = await self._wishlist_film()
        await db.update_film_hero(film_id, hero_url="https://kp/wide.jpg", hero_type="backdrop",
                                  hero_source="kinopoisk", hero_quality_score=0.935,
                                  hero_width=1920, hero_height=1080)
        item = (await main.wishlist_random(user={"id": 1}))["item"]
        self.assertEqual(item["hero_source"], "kinopoisk")
        self.assertEqual(item["hero_type"], "backdrop")
        self.assertEqual(item["hero_fit"], "contain")

    async def test_admin_can_update_fit_and_focus(self):
        film_id = await self._wishlist_film()
        await db.update_film_hero(
            film_id, hero_url="https://kp/wide.jpg", hero_type="backdrop",
            hero_source="kinopoisk", hero_quality_score=0.935,
            hero_width=1920, hero_height=1080)
        payload = await main.admin_update_hero_presentation(
            film_id,
            main.HeroPresentationPatch(fit="cover", focus_x=0.2, focus_y=0.8),
            user={"id": 1},
        )
        self.assertEqual(payload["hero_fit"], "cover")
        self.assertEqual((payload["hero_focus_x"], payload["hero_focus_y"]), (0.2, 0.8))

    async def test_non_admin_is_stopped_by_the_shared_server_gate(self):
        with mock.patch.object(deps, "_effective_role", new=mock.AsyncMock(return_value=None)):
            with self.assertRaises(main.HTTPException) as error:
                await main.require_editor(user={"id": 999})
        self.assertEqual(error.exception.status_code, 403)

    async def test_the_pipeline_never_accepts_what_the_proxy_will_not_serve(self):
        """Кадр, который наш же прокси откажется отдать, сохранять нельзя.

        Иначе в каталоге оседает изображение, которое физически невозможно
        показать: проверка приняла 12 МБ или image/svg+xml, а /img вернёт 413
        или 415. Эти два набора обязаны сходиться, и сходиться автоматически.
        """
        from enrichment import hero as hero_pipeline
        self.assertLessEqual(hero_pipeline._PROBE_MAX_BYTES, main._MAX_IMAGE_BYTES)
        self.assertTrue(hero_pipeline._ACCEPTED_IMAGE_TYPES.issubset(main._ALLOWED_IMG_TYPES))

    async def test_the_fanart_cdn_is_the_only_host_added_to_the_image_proxy(self):
        self.assertIn("assets.fanart.tv", main._ALLOWED_IMG_HOSTS)
        self.assertTrue(main._is_allowed_image_url("https://assets.fanart.tv/fanart/movies/1/a.jpg"))
        self.assertFalse(main._is_allowed_image_url("https://webservice.fanart.tv/v3.2/movies/tt1"))
        self.assertFalse(main._is_allowed_image_url("https://evil.example/a.jpg"))


if __name__ == "__main__":
    unittest.main()
