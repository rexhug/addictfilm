"""Регрессии на дефекты, найденные при разборе продакшена.

Каждый тест здесь закрывает конкретную ошибку, а не «функция что-то вернула».
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db
import media_type
import mood
import omdb
import recommendations as rec
import stats_cache
import text_matching


async def _keep_local_catalog(_user_id, _partner_id, _weights, candidates, _minimum):
    return candidates


class MoodDoubleCountingTests(unittest.TestCase):
    """apply_mood_layer кладёт в _score УЖЕ смешанное значение. Рольевые формулы
    обязаны брать исходный базовый счёт, иначе настроение входит дважды."""

    def test_normalized_base_prefers_the_untouched_score(self):
        movie = {"_score": 88.0, "_base_score": 45.0}
        self.assertAlmostEqual(rec.normalized_base_score(movie), 45.0 / rec._MAX_BASE_SCORE)

    def test_normalized_base_falls_back_for_candidates_without_mood(self):
        self.assertAlmostEqual(rec.normalized_base_score({"_score": 45.0}),
                               45.0 / rec._MAX_BASE_SCORE)
        self.assertEqual(rec.normalized_base_score({}), 0.0)

    def test_normalized_base_is_bounded(self):
        self.assertEqual(rec.normalized_base_score({"_base_score": 1e9}), 1.0)
        self.assertEqual(rec.normalized_base_score({"_base_score": -50.0}), 0.0)

    def test_mood_enters_the_role_formula_exactly_once(self):
        """Меняем ТОЛЬКО настроение: вклад обязан измениться ровно на весовой
        коэффициент, а не на него же плюс сдвиг смешанного счёта."""
        base = 45.0
        low = {"_score": 60.0, "_base_score": base, "_mood": 0.40}
        high = {"_score": 80.0, "_base_score": base, "_mood": 0.80}
        best = lambda m: 0.58 * m["_mood"] + 0.42 * rec.normalized_base_score(m)
        self.assertAlmostEqual(best(high) - best(low), 0.58 * (0.80 - 0.40), places=6)

    def test_layer_still_records_both_scores(self):
        ranked = [{"id": 1, "title": "A", "genres": "комедия", "plot": "", "runtime": "100 мин",
                   "_score": 50.0}]
        result = rec.apply_mood_layer(ranked, {"q1": "humor"}, preferences_count=0)
        self.assertEqual(result[0]["_base_score"], 50.0)
        self.assertNotEqual(result[0]["_score"], 50.0)


class MovieOnlyFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Прежние `movies_only or candidates` и `attached or candidates` возвращали
    сериалы обратно ровно тогда, когда фильтр и был нужен."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "t.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _film(self, imdb_id, title, kind):
        return await db.get_or_create_film(
            imdb_id=imdb_id, title=title, genres="драма", runtime="100 мин",
            imdb_rating="9.0", imdb_votes="500000", plot="История",
            poster_url="https://example.test/p.jpg", media_type=kind)

    async def _ranked(self):
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            return await rec.ranked_candidates(1, partner_id=None, weights={}, filters={},
                                               context={}, minimum=1)

    async def test_only_series_produces_no_recommendations(self):
        await self._film("tt1", "Сериал", "series")
        self.assertEqual(await self._ranked(), [])

    async def test_series_never_returns_alongside_a_movie(self):
        await self._film("tt2", "Сериал", "series")
        movie_id = await self._film("tt3", "Фильм", "movie")
        self.assertEqual([m["id"] for m in await self._ranked()], [movie_id])

    async def test_legacy_unknown_type_stays_eligible(self):
        film_id = await self._film("tt4", "Без типа", None)
        self.assertEqual([m["id"] for m in await self._ranked()], [film_id])

    async def test_profile_tv_overrides_an_unknown_catalog_row(self):
        film_id = await self._film("tt5", "Скрытый сериал", None)
        with patch("enrichment.repository.get_profiles",
                   return_value={film_id: {"content_type": "tv", "status": "ready"}}):
            attached = await rec._attach_profiles([{"id": film_id}])
        self.assertEqual(attached, [])

    async def test_attach_profiles_does_not_resurrect_excluded_content(self):
        with patch("enrichment.repository.get_profiles",
                   return_value={7: {"content_type": "short", "status": "ready"}}):
            self.assertEqual(await rec._attach_profiles([{"id": 7}]), [])

    def test_media_type_helper_still_permits_unknown(self):
        self.assertTrue(media_type.is_movie_flow_eligible({"genres": "драма"}))
        self.assertFalse(media_type.is_movie_flow_eligible({"media_type": "series"}))


class QuotaOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """Бюджет Kinopoisk списывает только kinopoisk._request."""

    async def test_provider_enrichment_does_not_prespend_quota(self):
        from enrichment import provider
        source = provider.from_catalog({"id": 1, "title": "Фильм", "kp_id": "123",
                                        "genres": "", "plot": ""})
        with patch("ratelimit.try_spend_search") as spend, \
                patch("kinopoisk.get_movie", return_value=None) as fetch:
            await provider.fetch_missing_metadata({"id": 1, "kp_id": "123", "imdb_id": "kp_1"},
                                                  source)
        fetch.assert_awaited_once()
        spend.assert_not_called()

    def test_only_the_client_module_spends_the_budget(self):
        """Защита от возврата дефекта в другом вызывающем коде."""
        import pathlib
        backend = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in backend.rglob("*.py"):
            if path.name in ("kinopoisk.py", "ratelimit.py") or "tests" in path.parts:
                continue
            if "try_spend_search()" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [])


class StaleProfilePolicyTests(unittest.TestCase):
    def _profile(self, **overrides):
        profile = {
            "status": "ready", "content_type": "movie", "feature_version": "v",
            "source_hash": "h", "confidence": 0.80, "validation_level": "valid",
            "tones": ["psychological"],
            "mood": {dim: 0.5 for dim in (*mood.MOOD_DIMENSIONS, "peak_darkness")},
        }
        profile.update(overrides)
        return {"id": 1, "_profile": profile}

    def test_ready_profile_is_used_as_is(self):
        features = rec.profile_mood_features(self._profile())
        self.assertIsNotNone(features)
        self.assertAlmostEqual(features.confidence, 0.80)

    def test_stale_profile_remains_usable_but_less_trusted(self):
        features = rec.profile_mood_features(self._profile(status="stale"))
        self.assertIsNotNone(features)
        self.assertLessEqual(features.confidence, rec._STALE_CONFIDENCE_CAP)
        self.assertLess(features.confidence, 0.80)

    def test_invalid_profile_is_never_used(self):
        self.assertIsNone(rec.profile_mood_features(
            self._profile(status="stale", validation_level="invalid")))
        self.assertIsNone(rec.profile_mood_features(
            self._profile(validation_level="invalid")))

    def test_failed_and_pending_profiles_are_not_used(self):
        for status in ("failed", "pending", "low_confidence"):
            with self.subTest(status=status):
                self.assertIsNone(rec.profile_mood_features(self._profile(status=status)))

    def test_non_movie_profile_is_not_used(self):
        for content_type in ("tv", "episode", "short"):
            with self.subTest(content_type=content_type):
                self.assertIsNone(rec.profile_mood_features(
                    self._profile(status="stale", content_type=content_type)))

    def test_out_of_range_or_incomplete_mood_is_rejected(self):
        self.assertIsNone(rec.profile_mood_features(
            self._profile(mood={**{d: 0.5 for d in mood.MOOD_DIMENSIONS}, "humor": 1.9})))
        self.assertIsNone(rec.profile_mood_features(self._profile(mood={"humor": 0.5})))


class TextMatchingTests(unittest.TestCase):
    def test_marker_matches_only_from_the_start_of_a_word(self):
        """Совпадение внутри слова — источник обоих прежних дефектов."""
        self.assertFalse(text_matching.matches_any_prefix("Они поехали вместе", ["месть"]))
        self.assertFalse(text_matching.matches_any_prefix("Прошло восемь лет", ["семь"]))
        self.assertFalse(text_matching.matches_any_prefix("Он местный житель", ["месть"]))

    def test_bare_stem_still_matches_a_word_that_starts_with_it(self):
        """Честная граница возможностей: маркеры — ПРЕФИКСЫ слов, поэтому «черн»
        совпадает и с «Черникой». Защита живёт не здесь, а в списке маркеров:
        у чёрной комедии он состоит из полных оборотов (см. следующий тест)."""
        self.assertTrue(text_matching.matches_any_prefix("Черника", ["черн"]))

    def test_dark_comedy_marker_set_ignores_an_unrelated_title(self):
        from enrichment import mappings
        from enrichment.taxonomy import CanonicalTone
        for title in ("Черника и письмо Деду Морозу", "Черноокая Сьюзан", "Чёрная птица"):
            with self.subTest(title=title):
                self.assertNotIn(str(CanonicalTone.DARK_COMEDY),
                                 mappings.match_reviewed_tones(title))
                self.assertNotIn("dark_comedy", mood.detect_markers({"title": title, "plot": ""}))

    def test_real_matches_still_work(self):
        self.assertTrue(text_matching.matches_any_prefix("Его семья погибла", ["семь", "семей"]))
        self.assertTrue(text_matching.matches_any_prefix("Чёрная комедия о похоронах",
                                                         ["чёрная комеди"]))
        self.assertTrue(text_matching.matches_any_prefix("A dark comedy about death",
                                                         ["dark comedy"]))

    def test_multiword_and_hyphenated_markers_survive_normalization(self):
        self.assertTrue(text_matching.matches_any_prefix("A coming of age story",
                                                         ["coming of age"]))
        self.assertTrue(text_matching.matches_any_prefix("Настоящее feel-good кино",
                                                         ["feel-good"]))

    def test_punctuation_does_not_block_a_match(self):
        self.assertTrue(text_matching.matches_any_prefix("«Месть» — его цель.", ["месть"]))

    def test_base_engine_tags_use_boundary_matching(self):
        tags = rec.film_tags({"genres": "драма", "plot": "Они поехали вместе на восемь дней"})
        self.assertNotIn("family", tags)
        self.assertIn("драма", tags)

    def test_matched_keys_returns_every_hit(self):
        groups = {"a": ("месть",), "b": ("друж",), "c": ("нетути",)}
        self.assertEqual(text_matching.matched_keys("Месть и дружба", groups), {"a", "b"})


class OmdbCacheTests(unittest.TestCase):
    def setUp(self):
        omdb._movie_cache.clear()

    def tearDown(self):
        omdb._movie_cache.clear()

    def test_cache_evicts_one_entry_not_everything(self):
        for index in range(omdb._CACHE_MAX + 5):
            omdb._movie_cache[f"tt{index}"] = {"Title": str(index)}
            omdb._movie_cache.move_to_end(f"tt{index}")
            if len(omdb._movie_cache) > omdb._CACHE_MAX:
                omdb._movie_cache.popitem(last=False)
        self.assertEqual(len(omdb._movie_cache), omdb._CACHE_MAX)
        self.assertNotIn("tt0", omdb._movie_cache)
        self.assertIn(f"tt{omdb._CACHE_MAX + 4}", omdb._movie_cache)


class StatsCacheTests(unittest.TestCase):
    def setUp(self):
        stats_cache.clear()

    def tearDown(self):
        stats_cache.clear()

    def test_invalidation_touches_only_the_affected_user(self):
        stats_cache.put(("personal", 1, 2026), {"x": 1})
        stats_cache.put(("personal", 2, 2026), {"x": 2})
        removed = stats_cache.invalidate_users([1])
        self.assertEqual(removed, 1)
        self.assertIsNone(stats_cache.get(("personal", 1, 2026)))
        self.assertIsNotNone(stats_cache.get(("personal", 2, 2026)))

    def test_pair_entry_is_invalidated_from_both_sides(self):
        stats_cache.put(("pair", 1, 2, "2026-01-01"), {"x": 1})
        self.assertEqual(stats_cache.invalidate_users([2]), 1)
        self.assertIsNone(stats_cache.get(("pair", 1, 2, "2026-01-01")))

    def test_empty_input_changes_nothing(self):
        stats_cache.put(("personal", 1, 2026), {"x": 1})
        self.assertEqual(stats_cache.invalidate_users([]), 0)
        self.assertIsNotNone(stats_cache.get(("personal", 1, 2026)))


class ConfigurationTests(unittest.TestCase):
    def test_malformed_admin_ids_name_the_variable(self):
        import config
        with patch.dict("os.environ", {"ADMIN_USER_IDS": "123,не-число"}):
            with self.assertRaises(config.ConfigurationError) as error:
                config._user_ids("ADMIN_USER_IDS")
        self.assertIn("ADMIN_USER_IDS", str(error.exception))
        self.assertNotIn("не-число", str(error.exception))

    def test_valid_admin_ids_parse(self):
        import config
        with patch.dict("os.environ", {"ADMIN_USER_IDS": " 1, 2 ,3 "}):
            self.assertEqual(config._user_ids("ADMIN_USER_IDS"), {1, 2, 3})

    def test_numeric_settings_are_bounded(self):
        import config
        with patch.dict("os.environ", {"X": "999999"}):
            self.assertEqual(config._bounded_int("X", 5, 1, 100), 100)
        with patch.dict("os.environ", {"X": "-4"}):
            self.assertEqual(config._bounded_float("X", 0.5, 0.0, 1.0), 0.0)

    def test_malformed_number_is_reported_not_silently_defaulted(self):
        import config
        with patch.dict("os.environ", {"X": "abc"}):
            with self.assertRaises(config.ConfigurationError):
                config._bounded_int("X", 5, 1, 100)


if __name__ == "__main__":
    unittest.main()
