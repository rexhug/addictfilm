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

    def _probe(self, verdict=None, *, width=1920, height=1080):
        return mock.patch.object(
            hero, "probe_image_info",
            new=mock.AsyncMock(return_value=hero.ImageProbeResult(
                verdict or hero.PROBE_OK, width, height)))

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

    async def test_presentation_validation_and_storage(self):
        film_id = await self._film()
        await db.update_film_hero(
            film_id, hero_url="https://assets.fanart.tv/a.jpg", hero_type="backdrop",
            hero_source="fanart", hero_quality_score=0.91,
            hero_width=1920, hero_height=1080)
        stored = await db.update_film_hero_presentation(
            film_id, fit="cover", focus_x=0.25, focus_y=0.75)
        self.assertEqual(stored["hero_fit"], "cover")
        self.assertEqual((stored["hero_focus_x"], stored["hero_focus_y"]), (0.25, 0.75))
        with self.assertRaises(ValueError):
            await db.update_film_hero_presentation(film_id, fit="smart")
        with self.assertRaises(ValueError):
            await db.update_film_hero_presentation(
                film_id, fit="cover", focus_x=1.01, focus_y=0.5)

    async def test_same_hero_preserves_but_changed_url_clears_presentation(self):
        film_id = await self._film()
        kwargs = {
            "hero_type": "backdrop", "hero_source": "fanart",
            "hero_quality_score": 0.91, "hero_width": 1920, "hero_height": 1080,
        }
        await db.update_film_hero(
            film_id, hero_url="https://assets.fanart.tv/a.jpg", **kwargs)
        await db.update_film_hero_presentation(
            film_id, fit="cover", focus_x=0.2, focus_y=0.7)
        same = await db.update_film_hero(
            film_id, hero_url="https://assets.fanart.tv/a.jpg", **kwargs)
        self.assertEqual(
            (same["hero_fit"], same["hero_focus_x"], same["hero_focus_y"]),
            ("cover", 0.2, 0.7))
        changed = await db.update_film_hero(
            film_id, hero_url="https://assets.fanart.tv/b.jpg", **kwargs)
        self.assertIsNone(changed["hero_fit"])
        self.assertIsNone(changed["hero_focus_x"])
        self.assertIsNone(changed["hero_focus_y"])

    async def test_poster_decision_binds_to_the_current_url(self):
        film_id = await self._film(poster="https://p/promo.jpg")
        rejected = await db.update_film_poster_display(
            film_id, state="rejected", reason="embedded_promotion")
        self.assertEqual(rejected["poster_display_url"], "https://p/promo.jpg")
        self.assertEqual(rejected["poster_reject_reason"], "embedded_promotion")
        self.assertTrue(hero_media.poster_is_rejected(rejected))
        async with db.db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "UPDATE films SET poster_url = ? WHERE id = ?",
                ("https://p/new.jpg", film_id))
            await conn.commit()
        changed = await db.get_film(film_id)
        self.assertFalse(hero_media.poster_is_rejected(changed))
        self.assertEqual(hero_media.hero_payload(changed)["hero_url"], "https://p/new.jpg")
        automatic = await db.update_film_poster_display(film_id, state="auto")
        self.assertIsNone(automatic["poster_display_url"])
        self.assertIsNone(automatic["poster_reject_reason"])

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
        with mock.patch.object(hero, "FANART_HERO_ENABLED", False), \
                self._fanart([image()]), self._probe():
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
        with self._fanart([image()]), self._probe():
            report = await hero.refresh_due_heroes(limit=10, dry_run=True)
        self.assertEqual(report.outcomes[0].action, hero.ACTION_DRY_RUN)
        film = await db.get_film(film_id)
        self.assertIsNone(film["hero_url"])
        self.assertIsNone(film["hero_checked_at"])

    async def test_dry_run_predicts_the_same_outcome_as_a_real_pass(self):
        """Осмотр обязан проверять файл так же, как настоящий проход.

        Реальный случай с прода: dry-run показал backdrop, а запись дала постер,
        потому что осмотр пропускал проверку файла. Такой осмотр бесполезен —
        именно на его основании принимается решение включать флаг.
        """
        await self._film()
        with self._fanart([image()]), self._probe(hero.PROBE_UNKNOWN):
            dry = await hero.refresh_due_heroes(limit=10, dry_run=True)
        self.assertEqual(dry.outcomes[0].action, hero.ACTION_UNAVAILABLE)
        self.assertIsNone(dry.outcomes[0].hero_type)

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


# По умолчанию — правдоподобный вес настоящего кадра: у фикстуры не должно быть
# свойств, которых не бывает у реального файла (заглушка весит на порядок меньше).
def _jpeg(width=1920, height=1080, size=300 * 1024) -> bytes:
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

    async def test_a_placeholder_plate_is_rejected_however_correct_its_size(self):
        """Реальный случай: kinopoisk отдаёт по обычной ссылке тёмную плашку со
        своим логотипом — честные 1920x1080 весом 15 КБ. Формально это картинка
        нужного размера, а на весь экран человек увидел бы серый квадрат."""
        verdict = await self._probe(body=_jpeg(size=15_233))
        self.assertEqual(verdict, hero.PROBE_REJECTED)

    async def test_a_real_photograph_passes_the_detail_floor(self):
        # Самый «лёгкий» настоящий кадр в каталоге — 247 КБ на 1920x1080.
        self.assertEqual(await self._probe(body=_jpeg(size=247 * 1024)), hero.PROBE_OK)

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
                mock.patch.object(hero, "probe_image_info",
                                  new=mock.AsyncMock(return_value=hero.ImageProbeResult(
                                      verdict, 1920, 1080))))

    async def test_three_consecutive_outages_open_the_circuit(self):
        await self._films(9)
        asked, patch_api, patch_probe = self._pipeline(hero.PROBE_UNKNOWN)
        with patch_api, patch_probe, self.assertLogs(hero.logger, level="WARNING"):
            report = await hero.refresh_due_heroes(limit=9, concurrency=3)
        self.assertTrue(hero._circuit_is_open())
        self.assertEqual(report.unavailable, 3)
        self.assertEqual(report.examined, 3, "остаток пачки не должен запрашиваться")
        self.assertEqual(len(asked), 3)

    async def test_an_open_circuit_makes_zero_fanart_calls(self):
        """Предохранитель гасит ТОЛЬКО Fanart. Фильмы всё равно обрабатываются:
        запасной постер и проверка kinopoisk от чужого сбоя не зависят."""
        await self._films(4)
        hero._open_circuit()
        called = mock.AsyncMock(return_value=[image()])
        with mock.patch.object(fanart, "get_movie_backgrounds", new=called):
            report = await hero.refresh_due_heroes(limit=4)
        called.assert_not_awaited()
        self.assertEqual(report.examined, 4)
        self.assertEqual(report.stored, 4, "постер должен сохраняться и при сбое Fanart")

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
            return hero.ImageProbeResult(verdict, 1920, 1080)

        async def _api(imdb_id, session=None):
            return [image()]

        with mock.patch.object(fanart, "get_movie_backgrounds", new=_api), \
                mock.patch.object(hero, "probe_image_info", new=_probe):
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


class KinopoiskHeroTests(unittest.IsolatedAsyncioTestCase):
    """Второй, независимый источник кадров.

    Смысл канала — не качество, а НЕЗАВИСИМОСТЬ: ссылка уже лежит в каталоге и
    не перестаёт работать, когда чужой сервис лежит. Но наличие backdrop_url
    по-прежнему ничего не доказывает: доказывает только скачанный файл.
    """

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "kp.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        hero.reset_circuit()
        self.addCleanup(hero.reset_circuit)
        for name, value in (("KINOPOISK_HERO_ENABLED", True), ("FANART_HERO_ENABLED", True)):
            patcher = mock.patch.object(hero, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _film(self, *, backdrop="https://kp/wide.jpg", poster="https://p/1.jpg") -> int:
        return await db.get_or_create_film(imdb_id="tt0000001", title="Фильм", genres="драма",
                                           media_type="movie", poster_url=poster,
                                           backdrop_url=backdrop, year="2010")

    def _probes(self, kinopoisk, fanart_result=None):
        """Разные ответы для разных URL: один вызов на kinopoisk, другой на CDN."""
        async def _probe(url, *, expected_width=None, expected_height=None, session=None):
            if url.startswith("https://kp/"):
                return kinopoisk
            return fanart_result or hero.ImageProbeResult(hero.PROBE_OK, 1920, 1080)
        return mock.patch.object(hero, "probe_image_info", new=_probe)

    def _fanart(self, images):
        calls: list[str] = []

        async def _api(imdb_id, session=None):
            calls.append(imdb_id)
            return list(images)
        return calls, mock.patch.object(fanart, "get_movie_backgrounds", new=_api)

    async def test_a_proven_kinopoisk_backdrop_is_stored(self):
        film_id = await self._film()
        _calls, patch_api = self._fanart([])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_OK, 1920, 1080)):
            report = await hero.refresh_due_heroes(limit=1)
        self.assertEqual(report.stored, 1)
        film = await db.get_film(film_id)
        self.assertEqual(film["hero_type"], "backdrop")
        self.assertEqual(film["hero_source"], "kinopoisk")
        self.assertEqual((film["hero_width"], film["hero_height"]), (1920, 1080))
        self.assertEqual(film["hero_url"], "https://kp/wide.jpg")

    async def test_a_small_kinopoisk_image_is_not_a_backdrop(self):
        film_id = await self._film()
        _calls, patch_api = self._fanart([])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_OK, 640, 360)):
            await hero.refresh_due_heroes(limit=1)
        self.assertEqual((await db.get_film(film_id))["hero_type"], "poster_blur")

    async def test_a_vertical_kinopoisk_image_is_not_a_backdrop(self):
        film_id = await self._film()
        _calls, patch_api = self._fanart([])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_OK, 1200, 1800)):
            await hero.refresh_due_heroes(limit=1)
        self.assertEqual((await db.get_film(film_id))["hero_type"], "poster_blur")

    async def test_unreadable_dimensions_are_not_proof(self):
        # Формат не распознан — «не доказано», а не «наверное подойдёт».
        film_id = await self._film()
        _calls, patch_api = self._fanart([])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_OK, None, None)):
            await hero.refresh_due_heroes(limit=1)
        self.assertEqual((await db.get_film(film_id))["hero_type"], "poster_blur")

    async def test_a_good_kinopoisk_backdrop_avoids_fanart_entirely(self):
        await self._film()
        calls, patch_api = self._fanart([image()])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_OK, 1920, 1080)):
            await hero.refresh_due_heroes(limit=1)
        self.assertEqual(calls, [], "лишний запрос к чужому сервису")

    async def test_a_rejected_kinopoisk_backdrop_falls_through_to_fanart(self):
        film_id = await self._film()
        calls, patch_api = self._fanart([image()])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_REJECTED)):
            await hero.refresh_due_heroes(limit=1)
        self.assertEqual(calls, ["tt0000001"])
        film = await db.get_film(film_id)
        self.assertEqual(film["hero_source"], "fanart")

    async def test_an_unavailable_kinopoisk_still_lets_fanart_work(self):
        film_id = await self._film()
        calls, patch_api = self._fanart([image()])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_UNKNOWN)):
            await hero.refresh_due_heroes(limit=1)
        self.assertEqual(calls, ["tt0000001"])
        self.assertEqual((await db.get_film(film_id))["hero_source"], "fanart")

    async def test_an_unavailable_fanart_still_lets_kinopoisk_work(self):
        film_id = await self._film()
        _calls, patch_api = self._fanart([image()])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_OK, 1920, 1080),
                                     hero.ImageProbeResult(hero.PROBE_UNKNOWN)):
            await hero.refresh_due_heroes(limit=1)
        self.assertEqual((await db.get_film(film_id))["hero_source"], "kinopoisk")

    async def test_both_sources_unavailable_writes_nothing(self):
        film_id = await self._film()
        _calls, patch_api = self._fanart([image()])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_UNKNOWN),
                                     hero.ImageProbeResult(hero.PROBE_UNKNOWN)):
            report = await hero.refresh_due_heroes(limit=1)
        self.assertEqual(report.unavailable, 1)
        film = await db.get_film(film_id)
        self.assertIsNone(film["hero_url"])
        self.assertIsNone(film["hero_checked_at"])

    async def test_both_sources_definitively_empty_falls_back_to_the_poster(self):
        film_id = await self._film()
        _calls, patch_api = self._fanart([])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_REJECTED)):
            await hero.refresh_due_heroes(limit=1)
        film = await db.get_film(film_id)
        self.assertEqual(film["hero_type"], "poster_blur")
        self.assertEqual(film["hero_source"], "poster")

    async def test_an_open_fanart_circuit_still_validates_kinopoisk(self):
        film_id = await self._film()
        hero._open_circuit()
        calls, patch_api = self._fanart([image()])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_OK, 1920, 1080)):
            await hero.refresh_due_heroes(limit=1)
        self.assertEqual(calls, [], "Fanart во время сбоя трогать нельзя")
        self.assertEqual((await db.get_film(film_id))["hero_source"], "kinopoisk")

    async def test_the_circuit_still_opens_when_kinopoisk_keeps_succeeding(self):
        """Успех kinopoisk не должен прятать затяжной сбой Fanart."""
        for index in range(3):
            await db.get_or_create_film(imdb_id=f"tt00001{index:02d}", title=f"Ф{index}",
                                        genres="драма", media_type="movie",
                                        poster_url="https://p/1.jpg",
                                        backdrop_url="https://kp/small.jpg", year="2010")
        _calls, patch_api = self._fanart([image()])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_OK, 640, 360),
                                     hero.ImageProbeResult(hero.PROBE_UNKNOWN)), \
                self.assertLogs(hero.logger, level="WARNING"):
            report = await hero.refresh_due_heroes(limit=3, concurrency=3)
        self.assertTrue(hero._circuit_is_open())
        self.assertEqual(report.unavailable, 3)

    async def test_a_stored_hero_survives_a_double_outage(self):
        film_id = await self._film()
        await db.update_film_hero(film_id, hero_url="https://kp/old.jpg", hero_type="backdrop",
                                  hero_source="kinopoisk", hero_quality_score=0.9,
                                  hero_width=1920, hero_height=1080)
        before = await db.get_film(film_id)
        _calls, patch_api = self._fanart([image()])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_UNKNOWN),
                                     hero.ImageProbeResult(hero.PROBE_UNKNOWN)):
            await hero.refresh_due_heroes(films=[before])
        after = await db.get_film(film_id)
        self.assertEqual(after["hero_url"], before["hero_url"])
        self.assertEqual(after["hero_checked_at"], before["hero_checked_at"])

    async def test_a_dry_run_writes_nothing(self):
        film_id = await self._film()
        _calls, patch_api = self._fanart([])
        with patch_api, self._probes(hero.ImageProbeResult(hero.PROBE_OK, 1920, 1080)):
            report = await hero.refresh_due_heroes(limit=1, dry_run=True)
        self.assertEqual(report.outcomes[0].hero_source, "kinopoisk")
        film = await db.get_film(film_id)
        self.assertIsNone(film["hero_url"])
        self.assertIsNone(film["hero_checked_at"])

    async def test_the_api_payload_reports_the_kinopoisk_source(self):
        import hero_media as policy
        film_id = await self._film()
        await db.update_film_hero(film_id, hero_url="https://kp/wide.jpg", hero_type="backdrop",
                                  hero_source="kinopoisk", hero_quality_score=0.935,
                                  hero_width=1920, hero_height=1080)
        payload = policy.hero_payload(await db.get_film(film_id))
        self.assertEqual(payload["hero_source"], "kinopoisk")
        self.assertEqual(payload["hero_type"], "backdrop")
        self.assertEqual(payload["hero_url"], "https://kp/wide.jpg")


class KinopoiskRenditionTests(unittest.IsolatedAsyncioTestCase):
    """Сохранённый в ссылке размер — это то, что вернул API, а не единственная
    доступная редакция. Замер на проде: 1344x756 → 383 КБ, 1920x1080 → 696 КБ
    (настоящие 1920x1080), orig → 3840x2160. Без запроса большей редакции весь
    канал давал бы ноль кадров: в каталоге 152 ссылки, и все на 1344x756."""

    def test_a_yandex_rendition_is_upgraded_first_and_falls_back_to_the_original(self):
        candidates = hero_media.kinopoisk_rendition_candidates(
            "https://avatars.mds.yandex.net/get-ott/239697/2a00000/1344x756")
        self.assertEqual(candidates, [
            "https://avatars.mds.yandex.net/get-ott/239697/2a00000/1920x1080",
            "https://avatars.mds.yandex.net/get-ott/239697/2a00000/1344x756",
        ])

    def test_an_already_preferred_rendition_is_not_duplicated(self):
        url = "https://avatars.mds.yandex.net/get-ott/239697/2a00000/1920x1080"
        self.assertEqual(hero_media.kinopoisk_rendition_candidates(url), [url])

    def test_a_foreign_host_is_never_rewritten(self):
        # Явное требование: TMDB как источник не подключаем. Его ссылки берём
        # ровно такими, какие уже лежат в каталоге.
        url = "https://image.tmdb.org/t/p/w1280/abc.jpg"
        self.assertEqual(hero_media.kinopoisk_rendition_candidates(url), [url])

    def test_a_url_without_a_size_directive_is_left_alone(self):
        url = "https://avatars.mds.yandex.net/get-ott/239697/2a00000"
        self.assertEqual(hero_media.kinopoisk_rendition_candidates(url), [url])

    def test_an_empty_url_yields_no_candidates(self):
        self.assertEqual(hero_media.kinopoisk_rendition_candidates(None), [])
        self.assertEqual(hero_media.kinopoisk_rendition_candidates("  "), [])


class KinopoiskRenditionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "rend.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        hero.reset_circuit()
        self.addCleanup(hero.reset_circuit)
        for name in ("KINOPOISK_HERO_ENABLED", "FANART_HERO_ENABLED"):
            patcher = mock.patch.object(hero, name, name == "KINOPOISK_HERO_ENABLED")
            patcher.start()
            self.addCleanup(patcher.stop)
        self.film_id = await db.get_or_create_film(
            imdb_id="tt0000001", title="Фильм", genres="драма", media_type="movie",
            poster_url="https://p/1.jpg", year="2010",
            backdrop_url="https://avatars.mds.yandex.net/get-ott/1/x/1344x756")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    def _probe(self, by_url):
        asked: list[str] = []

        async def _probe_info(url, *, expected_width=None, expected_height=None, session=None):
            asked.append(url)
            return by_url(url)
        return asked, mock.patch.object(hero, "probe_image_info", new=_probe_info)

    async def test_the_bigger_rendition_is_stored_when_it_really_is_bigger(self):
        asked, patch_probe = self._probe(
            lambda url: hero.ImageProbeResult(hero.PROBE_OK, 1920, 1080)
            if url.endswith("1920x1080") else hero.ImageProbeResult(hero.PROBE_OK, 1344, 756))
        with patch_probe:
            await hero.refresh_due_heroes(limit=1)
        film = await db.get_film(self.film_id)
        self.assertTrue(film["hero_url"].endswith("1920x1080"))
        self.assertEqual((film["hero_width"], film["hero_height"]), (1920, 1080))
        self.assertEqual(asked, ["https://avatars.mds.yandex.net/get-ott/1/x/1920x1080"],
                         "исходный размер спрашивать незачем — большой подошёл")

    async def test_a_missing_bigger_rendition_falls_back_to_the_stored_one(self):
        asked, patch_probe = self._probe(
            lambda url: hero.ImageProbeResult(hero.PROBE_REJECTED)
            if url.endswith("1920x1080") else hero.ImageProbeResult(hero.PROBE_OK, 1344, 756))
        with patch_probe:
            await hero.refresh_due_heroes(limit=1)
        self.assertEqual(len(asked), 2)
        # 1344x756 не проходит порог — честно уходим в постер, а не растягиваем.
        self.assertEqual((await db.get_film(self.film_id))["hero_type"], "poster_blur")

    async def test_a_network_failure_does_not_trigger_a_second_rendition_request(self):
        asked, patch_probe = self._probe(lambda _url: hero.ImageProbeResult(hero.PROBE_UNKNOWN))
        with patch_probe:
            report = await hero.refresh_due_heroes(limit=1)
        self.assertEqual(len(asked), 1, "сеть лежит — второй размер просить бессмысленно")
        self.assertEqual(report.unavailable, 1)
        self.assertIsNone((await db.get_film(self.film_id))["hero_checked_at"])


class SourceSelectionTests(unittest.IsolatedAsyncioTestCase):
    """Осмотр обязан показывать то, что сделает ПРОД.

    Реальный случай: dry-run лез в Fanart при FANART_HERO_ENABLED=0, четыре
    фильма получили «недоступно» из-за лежащего чужого CDN, и предохранитель
    оборвал порцию — при том, что в проде этих запросов не было бы вовсе.
    """

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "src.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        hero.reset_circuit()
        self.addCleanup(hero.reset_circuit)
        self.film_id = await db.get_or_create_film(
            imdb_id="tt0000001", title="Фильм", genres="драма", media_type="movie",
            poster_url="https://p/1.jpg", year="2010", backdrop_url="https://kp/wide.jpg")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    def _flags(self, *, kinopoisk, fanart_flag):
        return (mock.patch.object(hero, "KINOPOISK_HERO_ENABLED", kinopoisk),
                mock.patch.object(hero, "FANART_HERO_ENABLED", fanart_flag))

    def test_a_dry_run_follows_the_enabled_sources(self):
        kp, fa = self._flags(kinopoisk=True, fanart_flag=False)
        with kp, fa:
            self.assertEqual(hero.resolve_sources(True), (True, False))
            self.assertEqual(hero.resolve_sources(False), (True, False))

    def test_a_dry_run_before_the_first_enable_inspects_everything(self):
        kp, fa = self._flags(kinopoisk=False, fanart_flag=False)
        with kp, fa:
            self.assertEqual(hero.resolve_sources(True), (True, True))
            self.assertEqual(hero.resolve_sources(False), (False, False))

    def test_an_explicit_source_overrides_the_flags(self):
        kp, fa = self._flags(kinopoisk=True, fanart_flag=True)
        with kp, fa:
            self.assertEqual(hero.resolve_sources(True, "kinopoisk"), (True, False))
            self.assertEqual(hero.resolve_sources(True, "fanart"), (False, True))
            self.assertEqual(hero.resolve_sources(True, "all"), (True, True))

    async def test_a_kinopoisk_only_run_never_touches_fanart(self):
        called = mock.AsyncMock(return_value=[image()])
        probe = mock.AsyncMock(return_value=hero.ImageProbeResult(hero.PROBE_REJECTED))
        kp, fa = self._flags(kinopoisk=False, fanart_flag=False)
        with kp, fa, mock.patch.object(fanart, "get_movie_backgrounds", new=called), \
                mock.patch.object(hero, "probe_image_info", new=probe):
            report = await hero.refresh_due_heroes(limit=1, dry_run=True, sources="kinopoisk")
        called.assert_not_awaited()
        self.assertEqual(report.examined, 1)


if __name__ == "__main__":
    unittest.main()
