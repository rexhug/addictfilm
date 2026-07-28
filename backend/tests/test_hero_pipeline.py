"""Хранение выбранного кадра, обновление воркером и выдача в API.

Главное, что здесь защищается: внешний сервис не должен вызываться на пути
пользовательского запроса, а второй проход подряд не должен делать НИ ОДНОГО
внешнего запроса — иначе каждый деплой превращался бы в обход каталога.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import aiohttp
import database as db
import fanart
import hero_media
from enrichment import hero


def image(width=1920, height=1080, likes=12, language="00", ident="a"):
    return fanart.FanartImage(id=ident, url=f"https://assets.fanart.tv/{ident}.jpg",
                              language=language, likes=likes, width=width, height=height)


class HeroPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "hero.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        self.flag = mock.patch.object(hero, "FANART_HERO_ENABLED", True)
        self.flag.start()
        self.addCleanup(self.flag.stop)

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _film(self, imdb_id="tt0000001", *, poster="https://p/1.jpg", year="2010") -> int:
        return await db.get_or_create_film(imdb_id=imdb_id, title=f"Фильм {imdb_id}",
                                           genres="драма", media_type="movie",
                                           poster_url=poster, year=year)

    def _fanart(self, images, *, error=None):
        async def _fake(imdb_id, session=None):
            if error:
                raise error
            return list(images)
        return mock.patch.object(fanart, "get_movie_backgrounds", new=_fake)

    def _probe(self, verdict=None):
        return mock.patch.object(
            hero, "probe_image",
            new=mock.AsyncMock(return_value=verdict or hero.PROBE_OK))

    # ── схема ────────────────────────────────────────────────────────────────
    async def test_migration_is_idempotent(self):
        await db.init_db()
        await db.init_db()
        film_id = await self._film()
        self.assertIsNotNone(await db.get_film(film_id))

    async def test_a_legacy_row_without_hero_columns_stays_readable(self):
        film_id = await self._film()
        film = await db.get_film(film_id)
        self.assertIsNone(film["hero_url"])
        self.assertIsNone(film["hero_checked_at"])
        self.assertEqual(hero_media.hero_payload(film)["hero_type"], "poster_blur")

    async def test_update_stores_every_field_at_once(self):
        film_id = await self._film()
        stored = await db.update_film_hero(
            film_id, hero_url="https://assets.fanart.tv/a.jpg", hero_type="backdrop",
            hero_source="fanart", hero_quality_score=0.91, hero_width=1920, hero_height=1080)
        self.assertEqual(stored["hero_url"], "https://assets.fanart.tv/a.jpg")
        self.assertEqual((stored["hero_width"], stored["hero_height"]), (1920, 1080))
        self.assertEqual(stored["hero_source"], "fanart")
        self.assertAlmostEqual(stored["hero_quality_score"], 0.91)
        self.assertIsNotNone(stored["hero_updated_at"])
        self.assertIsNotNone(stored["hero_checked_at"])

    async def test_an_invalid_type_or_source_is_refused(self):
        film_id = await self._film()
        with self.assertRaises(ValueError):
            await db.update_film_hero(film_id, hero_url="u", hero_type="cinema",
                                      hero_source="fanart", hero_quality_score=0.9,
                                      hero_width=None, hero_height=None)
        with self.assertRaises(ValueError):
            await db.update_film_hero(film_id, hero_url="u", hero_type="backdrop",
                                      hero_source="tmdb", hero_quality_score=0.9,
                                      hero_width=None, hero_height=None)

    # ── отбор кандидатов ─────────────────────────────────────────────────────
    async def test_never_checked_films_come_first(self):
        await self._film("tt0000001")
        second = await self._film("tt0000002")
        await db.update_film_hero(second, hero_url="https://assets.fanart.tv/a.jpg",
                                  hero_type="backdrop", hero_source="fanart",
                                  hero_quality_score=0.9, hero_width=1920, hero_height=1080)
        candidates = await db.list_films_missing_or_stale_hero(limit=10)
        self.assertEqual([film["imdb_id"] for film in candidates], ["tt0000001"])

    async def test_a_strong_recent_selection_is_not_rechecked(self):
        film_id = await self._film()
        await db.update_film_hero(film_id, hero_url="https://assets.fanart.tv/a.jpg",
                                  hero_type="backdrop", hero_source="fanart",
                                  hero_quality_score=0.92, hero_width=1920, hero_height=1080)
        soon = datetime.now(UTC) + timedelta(days=45)
        self.assertEqual(await db.list_films_missing_or_stale_hero(limit=10, now=soon), [])
        later = datetime.now(UTC) + timedelta(days=95)
        self.assertEqual(len(await db.list_films_missing_or_stale_hero(limit=10, now=later)), 1)

    async def test_a_weaker_selection_is_rechecked_sooner(self):
        film_id = await self._film()
        await db.update_film_hero(film_id, hero_url="https://assets.fanart.tv/a.jpg",
                                  hero_type="backdrop", hero_source="fanart",
                                  hero_quality_score=0.75, hero_width=1600, hero_height=900)
        moment = datetime.now(UTC) + timedelta(days=35)
        self.assertEqual(len(await db.list_films_missing_or_stale_hero(limit=10, now=moment)), 1)

    async def test_a_fallback_on_a_recent_film_is_rechecked_weekly(self):
        recent = str(datetime.now(UTC).year)
        film_id = await self._film("tt0000009", year=recent)
        await db.update_film_hero(film_id, hero_url="https://p/1.jpg", hero_type="poster_blur",
                                  hero_source="poster", hero_quality_score=0.5,
                                  hero_width=None, hero_height=None)
        after_week = datetime.now(UTC) + timedelta(days=8)
        self.assertEqual(len(await db.list_films_missing_or_stale_hero(limit=10, now=after_week)), 1)
        after_two_days = datetime.now(UTC) + timedelta(days=2)
        self.assertEqual(await db.list_films_missing_or_stale_hero(limit=10, now=after_two_days), [])

    async def test_a_fallback_on_an_old_film_waits_a_month(self):
        film_id = await self._film("tt0000010", year="1994")
        await db.update_film_hero(film_id, hero_url="https://p/1.jpg", hero_type="poster_blur",
                                  hero_source="poster", hero_quality_score=0.5,
                                  hero_width=None, hero_height=None)
        after_week = datetime.now(UTC) + timedelta(days=8)
        self.assertEqual(await db.list_films_missing_or_stale_hero(limit=10, now=after_week), [])
        after_month = datetime.now(UTC) + timedelta(days=32)
        self.assertEqual(len(await db.list_films_missing_or_stale_hero(limit=10, now=after_month)), 1)

    # ── обогащение ───────────────────────────────────────────────────────────
    async def test_the_flag_off_means_no_external_call_at_all(self):
        await self._film()
        called = mock.AsyncMock(return_value=[image()])
        with mock.patch.object(hero, "FANART_HERO_ENABLED", False), \
             mock.patch.object(fanart, "get_movie_backgrounds", new=called):
            report = await hero.refresh_due_heroes(limit=10)
        self.assertEqual(report.examined, 0)
        called.assert_not_awaited()

    async def test_a_dry_run_is_allowed_before_the_flag_is_turned_on(self):
        # Иначе флаг пришлось бы включать вслепую: посмотреть на качество отбора
        # было бы нечем, а осмотр ничего не записывает.
        film_id = await self._film()
        with mock.patch.object(hero, "FANART_HERO_ENABLED", False), self._fanart([image()]):
            report = await hero.refresh_due_heroes(limit=10, dry_run=True)
        self.assertEqual(report.examined, 1)
        self.assertEqual(report.outcomes[0].hero_type, "backdrop")
        self.assertIsNone((await db.get_film(film_id))["hero_checked_at"])

    async def test_a_qualified_backdrop_is_stored(self):
        film_id = await self._film()
        with self._fanart([image()]), self._probe():
            report = await hero.refresh_due_heroes(limit=10)
        self.assertEqual(report.stored, 1)
        film = await db.get_film(film_id)
        self.assertEqual(film["hero_type"], "backdrop")
        self.assertEqual(film["hero_source"], "fanart")

    async def test_no_artwork_selects_the_poster_fallback(self):
        film_id = await self._film()
        with self._fanart([]):
            await hero.refresh_due_heroes(limit=10)
        film = await db.get_film(film_id)
        self.assertEqual(film["hero_type"], "poster_blur")
        self.assertEqual(film["hero_url"], "https://p/1.jpg")

    async def test_a_file_that_is_received_and_unusable_falls_back(self):
        film_id = await self._film()
        with self._fanart([image()]), self._probe(hero.PROBE_REJECTED):
            await hero.refresh_due_heroes(limit=10)
        film = await db.get_film(film_id)
        self.assertEqual(film["hero_type"], "poster_blur")

    async def test_an_unreachable_cdn_changes_nothing_at_all(self):
        """Реальный случай с прода: CDN Fanart лежал. Записывать запасной постер
        и отметку проверки нельзя — фильм заморозился бы на недели из-за чужого
        получасового сбоя."""
        film_id = await self._film()
        with self._fanart([image()]), self._probe(hero.PROBE_UNKNOWN):
            report = await hero.refresh_due_heroes(limit=10)
        self.assertEqual(report.unavailable, 1)
        self.assertEqual(report.stored, 0)
        film = await db.get_film(film_id)
        self.assertIsNone(film["hero_url"])
        self.assertIsNone(film["hero_checked_at"])

    async def test_a_film_stays_a_candidate_after_an_unreachable_cdn(self):
        await self._film()
        with self._fanart([image()]), self._probe(hero.PROBE_UNKNOWN):
            await hero.refresh_due_heroes(limit=10)
        self.assertEqual(len(await db.list_films_missing_or_stale_hero(limit=10)), 1)

    async def test_a_temporary_outage_preserves_the_existing_selection(self):
        film_id = await self._film()
        await db.update_film_hero(film_id, hero_url="https://assets.fanart.tv/old.jpg",
                                  hero_type="backdrop", hero_source="fanart",
                                  hero_quality_score=0.9, hero_width=1920, hero_height=1080)
        before = await db.get_film(film_id)
        with self._fanart([], error=fanart.FanartUnavailable("down")):
            report = await hero.refresh_due_heroes(films=[before])
        self.assertEqual(report.unavailable, 1)
        after = await db.get_film(film_id)
        self.assertEqual(after["hero_url"], before["hero_url"])
        self.assertEqual(after["hero_checked_at"], before["hero_checked_at"])

    async def test_a_film_with_nothing_to_choose_from_is_marked_as_checked(self):
        # Ни IMDb-id, ни постера: без отметки такой фильм возвращался бы в
        # кандидаты бесконечно и стучался бы наружу на каждом круге воркера.
        film_id = await db.get_or_create_film(imdb_id="local-1", title="Без постера",
                                              media_type="movie", poster_url=None)
        with self._fanart([]):
            report = await hero.refresh_due_heroes(limit=10)
        self.assertEqual(report.without_hero, 1)
        self.assertIsNotNone((await db.get_film(film_id))["hero_checked_at"])

    async def test_a_second_pass_makes_no_external_request(self):
        await self._film()
        with self._fanart([image()]), self._probe():
            await hero.refresh_due_heroes(limit=10)
        called = mock.AsyncMock(return_value=[image()])
        with mock.patch.object(fanart, "get_movie_backgrounds", new=called):
            report = await hero.refresh_due_heroes(limit=10)
        self.assertEqual(report.examined, 0)
        called.assert_not_awaited()

    async def test_repeating_the_same_choice_does_not_move_the_update_stamp(self):
        film_id = await self._film()
        with self._fanart([image()]), self._probe():
            await hero.refresh_due_heroes(limit=10)
        first = await db.get_film(film_id)
        with self._fanart([image()]), self._probe():
            report = await hero.refresh_due_heroes(films=[first])
        second = await db.get_film(film_id)
        self.assertEqual(report.unchanged, 1)
        self.assertEqual(second["hero_updated_at"], first["hero_updated_at"])
        self.assertNotEqual(second["hero_checked_at"], first["hero_checked_at"])

    async def test_one_failing_film_does_not_stop_the_batch(self):
        first = await self._film("tt0000001")
        second = await self._film("tt0000002")
        calls = {"n": 0}

        async def _flaky(imdb_id, session=None):
            calls["n"] += 1
            if imdb_id == "tt0000001":
                raise RuntimeError("что-то пошло не так")
            return [image()]

        with mock.patch.object(fanart, "get_movie_backgrounds", new=_flaky), self._probe(), \
                self.assertLogs(hero.logger, level="WARNING"):
            report = await hero.refresh_due_heroes(limit=10)
        self.assertEqual(report.examined, 2)
        self.assertIsNone((await db.get_film(first))["hero_url"])
        self.assertEqual((await db.get_film(second))["hero_type"], "backdrop")

    async def test_concurrency_stays_within_the_configured_bound(self):
        for index in range(6):
            await self._film(f"tt000010{index}")
        live, peak = {"now": 0, "max": 0}, 0

        async def _slow(imdb_id, session=None):
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            try:
                return [image()]
            finally:
                live["now"] -= 1

        with mock.patch.object(fanart, "get_movie_backgrounds", new=_slow), self._probe():
            await hero.refresh_due_heroes(limit=10, concurrency=2)
        peak = live["max"]
        self.assertLessEqual(peak, 2)

    async def test_dry_run_changes_nothing(self):
        film_id = await self._film()
        with self._fanart([image()]):
            report = await hero.refresh_due_heroes(limit=10, dry_run=True)
        self.assertEqual(report.outcomes[0].action, hero.ACTION_DRY_RUN)
        film = await db.get_film(film_id)
        self.assertIsNone(film["hero_url"])
        self.assertIsNone(film["hero_checked_at"])

    async def test_distribution_reports_what_is_actually_stored(self):
        first = await self._film("tt0000001")
        await self._film("tt0000002")
        await db.update_film_hero(first, hero_url="https://assets.fanart.tv/a.jpg",
                                  hero_type="backdrop", hero_source="fanart",
                                  hero_quality_score=0.9, hero_width=1920, hero_height=1080)
        distribution = await db.hero_distribution()
        self.assertEqual(distribution["films"], 2)
        self.assertEqual(distribution["checked"], 1)
        self.assertEqual(distribution["by_source"]["fanart"]["count"], 1)
        self.assertEqual(distribution["by_source"]["none"]["count"], 1)


class ImageProbeTests(unittest.TestCase):
    """Проверка файла — единственное место, где мы вообще качаем байты."""

    def test_png_dimensions_are_read_from_the_header(self):
        head = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big")
        self.assertEqual(hero._sniff_dimensions(head), (1920, 1080))

    def test_jpeg_dimensions_are_read_from_the_frame_header(self):
        head = (b"\xff\xd8" + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
                + (1080).to_bytes(2, "big") + (1920).to_bytes(2, "big"))
        self.assertEqual(hero._sniff_dimensions(head), (1920, 1080))

    def test_an_unknown_format_is_not_a_failure(self):
        self.assertIsNone(hero._sniff_dimensions(b"GIF89a not really"))


class _ProbeResponse:
    def __init__(self, status=200, ctype="image/jpeg", body=b"", error=None):
        self.status = status
        self.headers = {"Content-Type": ctype}
        self._body = body
        self._error = error

    async def __aenter__(self):
        if self._error:
            raise self._error
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def content(self):
        chunks = [self._body]

        class _Reader:
            @staticmethod
            async def iter_chunked(_size):
                for chunk in chunks:
                    yield chunk
        return _Reader()


class _ProbeSession:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def get(self, _url):
        self.calls += 1
        return self._response


def _jpeg(width=1920, height=1080, size=8192) -> bytes:
    head = (b"\xff\xd8\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
            + height.to_bytes(2, "big") + width.to_bytes(2, "big"))
    return head + b"\x00" * max(0, size - len(head))


class ProbeStatusTests(unittest.IsolatedAsyncioTestCase):
    """Ответ CDN делится не на «успех/неуспех», а на «это про файл» и «это про
    то, что мы сейчас не смогли его получить»."""

    async def _probe(self, **kwargs):
        session = _ProbeSession(_ProbeResponse(**kwargs))
        return await hero.probe_image("https://assets.fanart.tv/a.jpg",
                                      expected_width=1920, expected_height=1080,
                                      session=session)

    async def test_transient_statuses_never_condemn_the_file(self):
        # 401/403 здесь тоже временные: у CDN картинок нет нашей авторизации,
        # и такой ответ означает защиту от нагрузки, а не отсутствие файла.
        for status in (401, 403, 408, 425, 429, 500, 502, 503):
            self.assertEqual(await self._probe(status=status), hero.PROBE_UNKNOWN, status)

    async def test_a_proven_absence_is_a_rejection(self):
        for status in (404, 410):
            self.assertEqual(await self._probe(status=status), hero.PROBE_REJECTED, status)

    async def test_a_network_failure_is_unknown(self):
        for error in (TimeoutError(), aiohttp.ClientError("boom")):
            self.assertEqual(await self._probe(error=error), hero.PROBE_UNKNOWN,
                             type(error).__name__)

    async def test_a_non_image_answer_with_status_200_is_rejected(self):
        self.assertEqual(await self._probe(ctype="text/html", body=_jpeg()),
                         hero.PROBE_REJECTED)

    async def test_a_truncated_file_is_rejected(self):
        self.assertEqual(await self._probe(body=b"\xff\xd8" + b"\x00" * 100),
                         hero.PROBE_REJECTED)

    async def test_dimensions_that_contradict_the_metadata_are_rejected(self):
        with self.assertLogs(hero.logger, level="WARNING"):
            verdict = await self._probe(body=_jpeg(width=640, height=360))
        self.assertEqual(verdict, hero.PROBE_REJECTED)

    async def test_a_matching_file_passes(self):
        self.assertEqual(await self._probe(body=_jpeg()), hero.PROBE_OK)


class CircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    """Глобальный сбой Fanart не должен превращаться в двадцать одинаково
    безрезультатных запросов каждые пятнадцать минут."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "circuit.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        hero.reset_circuit()
        self.addCleanup(hero.reset_circuit)
        self.flag = mock.patch.object(hero, "FANART_HERO_ENABLED", True)
        self.flag.start()
        self.addCleanup(self.flag.stop)

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _films(self, count: int) -> None:
        for index in range(count):
            await db.get_or_create_film(imdb_id=f"tt900{index:03d}", title=f"Фильм {index}",
                                        genres="драма", media_type="movie",
                                        poster_url="https://p/1.jpg", year="2010")

    def _pipeline(self, verdict):
        """Возвращает список фильмов, у которых ВООБЩЕ спросили Fanart."""
        asked: list[str] = []

        async def _api(imdb_id, session=None):
            asked.append(imdb_id)
            return [image()]

        return (asked,
                mock.patch.object(fanart, "get_movie_backgrounds", new=_api),
                mock.patch.object(hero, "probe_image",
                                  new=mock.AsyncMock(return_value=verdict)))

    async def test_three_consecutive_outages_open_the_circuit(self):
        await self._films(9)
        asked, patch_api, patch_probe = self._pipeline(hero.PROBE_UNKNOWN)
        with patch_api, patch_probe, self.assertLogs(hero.logger, level="WARNING"):
            report = await hero.refresh_due_heroes(limit=9, concurrency=3)
        self.assertTrue(hero._circuit_is_open())
        self.assertEqual(report.unavailable, 3)
        self.assertEqual(report.examined, 3, "остаток пачки не должен запрашиваться")
        self.assertEqual(len(asked), 3)

    async def test_an_open_circuit_makes_zero_external_calls(self):
        await self._films(4)
        hero._open_circuit()
        called = mock.AsyncMock(return_value=[image()])
        with mock.patch.object(fanart, "get_movie_backgrounds", new=called), \
                self.assertLogs(hero.logger, level="WARNING"):
            report = await hero.refresh_due_heroes(limit=4)
        self.assertEqual(report.examined, 0)
        called.assert_not_awaited()

    async def test_an_outage_leaves_every_hero_column_untouched(self):
        await self._films(3)
        _asked, patch_api, patch_probe = self._pipeline(hero.PROBE_UNKNOWN)
        with patch_api, patch_probe, self.assertLogs(hero.logger, level="WARNING"):
            await hero.refresh_due_heroes(limit=3, concurrency=3)
        for film in await db.list_films_for_hero_backfill(limit=3):
            self.assertIsNone(film["hero_url"])
            self.assertIsNone(film["hero_type"])
            self.assertIsNone(film["hero_checked_at"])
            self.assertIsNone(film["hero_updated_at"])

    async def test_a_stored_hero_survives_an_outage(self):
        await self._films(1)
        film_id = (await db.list_films_for_hero_backfill(limit=1))[0]["id"]
        await db.update_film_hero(film_id, hero_url="https://assets.fanart.tv/old.jpg",
                                  hero_type="backdrop", hero_source="fanart",
                                  hero_quality_score=0.9, hero_width=1920, hero_height=1080)
        before = await db.get_film(film_id)
        _asked, patch_api, patch_probe = self._pipeline(hero.PROBE_UNKNOWN)
        with patch_api, patch_probe:
            await hero.refresh_due_heroes(films=[before])
        after = await db.get_film(film_id)
        self.assertEqual(after["hero_url"], before["hero_url"])
        self.assertEqual(after["hero_checked_at"], before["hero_checked_at"])

    async def test_a_definite_answer_resets_the_counter(self):
        await self._films(6)
        verdicts = [hero.PROBE_UNKNOWN, hero.PROBE_UNKNOWN, hero.PROBE_OK,
                    hero.PROBE_UNKNOWN, hero.PROBE_UNKNOWN, hero.PROBE_OK]
        calls = {"n": 0}

        async def _probe(*_args, **_kwargs):
            verdict = verdicts[min(calls["n"], len(verdicts) - 1)]
            calls["n"] += 1
            return verdict

        async def _api(imdb_id, session=None):
            return [image()]

        with mock.patch.object(fanart, "get_movie_backgrounds", new=_api), \
                mock.patch.object(hero, "probe_image", new=_probe):
            report = await hero.refresh_due_heroes(limit=6, concurrency=1)
        self.assertFalse(hero._circuit_is_open(), "трёх подряд так и не случилось")
        self.assertEqual(report.examined, 6)
        self.assertEqual(report.unavailable, 4)
        self.assertEqual(report.stored, 2)

    async def test_a_dry_run_still_works_with_the_flag_off(self):
        await self._films(2)
        _asked, patch_api, patch_probe = self._pipeline(hero.PROBE_OK)
        with mock.patch.object(hero, "FANART_HERO_ENABLED", False), patch_api, patch_probe:
            report = await hero.refresh_due_heroes(limit=2, dry_run=True)
        self.assertEqual(report.examined, 2)
        self.assertTrue(all(o.action == hero.ACTION_DRY_RUN for o in report.outcomes))


if __name__ == "__main__":
    unittest.main()
