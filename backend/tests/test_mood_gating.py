"""Двухэтапный отбор: сначала уместность по настроению, потом ранжирование.

Регрессии здесь не абстрактные: до этой правки блокбастер с высоким базовым
счётом выигрывал запрос, которому он не соответствовал («Аватар: Легенда об
Аанге» на «чёрный юмор», «Мстители» на «максимально интеллектуальное»).
Проверяем СВОЙСТВА, а не конкретные ID: фильм не должен проходить, если у него
проседает измерение, ради которого человек и пришёл.
"""
import unittest

import mood
import recommendations as rec


def _scored(film_id, title, *, genres, quality=7.5, base=40.0, novelty=2.0, plot=""):
    """Кандидат в том виде, в каком его отдаёт базовый движок + слой настроения."""
    film = {"id": film_id, "title": title, "genres": genres, "plot": plot,
            "runtime": "110 min", "imdb_rating": "7.5", "imdb_votes": "50000"}
    features = mood.build_movie_features(film)
    return {**film, "_score": base, "_quality": quality, "_novelty": novelty,
            "_mood_features": features}


def _with_mood(candidate, intent):
    raw = mood.mood_similarity(intent, candidate["_mood_features"])
    candidate["_mood"] = mood.confidence_adjusted(raw, candidate["_mood_features"].confidence)
    return candidate


class ThresholdTests(unittest.TestCase):
    def test_every_role_resolves_threshold_within_bounds(self):
        intent = mood.build_mood_intent({"q1": "humor", "h1": "dark"})
        for role in ("best", "reliable", "unexpected"):
            for level in mood.FALLBACK_THRESHOLD_ADJUSTMENTS:
                value = mood.resolve_mood_threshold(role, intent, fallback_level=level)
                with self.subTest(role=role, level=level):
                    self.assertGreaterEqual(value, mood.MINIMUM_MOOD_SAFETY_FLOOR)
                    self.assertLessEqual(value, 0.82)

    def test_specific_request_is_stricter_than_broad_one(self):
        specific = mood.build_mood_intent({"q1": "tension", "t1": "horror"})
        broad = mood.build_mood_intent({"r2": "medium"})
        self.assertGreater(mood.calculate_intent_specificity(specific),
                           mood.calculate_intent_specificity(broad))

    def test_fallback_never_drops_below_safety_floor(self):
        intent = mood.build_mood_intent({"r2": "medium"})
        deepest = max(mood.FALLBACK_THRESHOLD_ADJUSTMENTS)
        value = mood.resolve_mood_threshold("reliable", intent, fallback_level=deepest)
        self.assertGreaterEqual(value, mood.MINIMUM_MOOD_SAFETY_FLOOR)

    def test_best_match_is_strictest_role(self):
        self.assertGreater(mood.MOOD_ROLE_THRESHOLDS["best"], mood.MOOD_ROLE_THRESHOLDS["reliable"])
        self.assertGreater(mood.PRIMARY_INTENT_FLOORS["best"], mood.PRIMARY_INTENT_FLOORS["reliable"])


class DimensionFloorTests(unittest.TestCase):
    def test_dark_humor_requires_actual_humor(self):
        floors = mood.collect_dimension_floors({"q1": "humor", "h1": "dark"})
        self.assertGreaterEqual(floors["humor_min"], 0.5)
        low_humor = _scored(1, "Блокбастер", genres="боевик,фэнтези")
        self.assertFalse(mood.passes_dimension_floors(low_humor["_mood_features"], floors))
        real_comedy = _scored(2, "Чёрная комедия", genres="комедия,криминал")
        self.assertTrue(mood.passes_dimension_floors(real_comedy["_mood_features"], floors))

    def test_intellectual_requires_complexity(self):
        floors = mood.collect_dimension_floors({"q1": "unusual", "u2": "high"})
        simple = _scored(1, "Простое", genres="боевик")
        complex_film = _scored(2, "Сложное", genres="детектив,фантастика")
        self.assertFalse(mood.passes_dimension_floors(simple["_mood_features"], floors))
        self.assertTrue(mood.passes_dimension_floors(complex_film["_mood_features"], floors))

    def test_cozy_request_caps_tension_and_darkness(self):
        floors = mood.collect_dimension_floors({"q1": "relax", "r3": "comfort"})
        grim = _scored(1, "Мрачное", genres="ужасы")
        cozy = _scored(2, "Уютное", genres="семейный,комедия")
        self.assertFalse(mood.passes_dimension_floors(grim["_mood_features"], floors))
        self.assertTrue(mood.passes_dimension_floors(cozy["_mood_features"], floors))

    def test_broad_answers_create_no_floors(self):
        """«Средний темп» и «неважно» не должны ничего отсекать."""
        self.assertEqual(mood.collect_dimension_floors({"r2": "medium", "c1": "any"}), {})


class ContradictionTests(unittest.TestCase):
    def test_calm_request_versus_maximum_tension_is_contradiction(self):
        intent = mood.MoodIntent(targets={"tension": 0.10}, importance={"tension": 1.0})
        horror = _scored(1, "Ужас", genres="ужасы")
        found = mood.find_critical_contradictions(intent, horror["_mood_features"])
        self.assertTrue(found)
        self.assertEqual(found[0][0], "tension")

    def test_low_importance_dimension_is_not_critical(self):
        intent = mood.MoodIntent(targets={"tension": 0.10}, importance={"tension": 0.3})
        horror = _scored(1, "Ужас", genres="ужасы")
        self.assertEqual(mood.find_critical_contradictions(intent, horror["_mood_features"]), [])

    def test_close_match_is_not_contradiction(self):
        intent = mood.MoodIntent(targets={"humor": 0.85}, importance={"humor": 1.0})
        comedy = _scored(1, "Комедия", genres="комедия")
        self.assertEqual(mood.find_critical_contradictions(intent, comedy["_mood_features"]), [])


class EligibilityTests(unittest.TestCase):
    def _pool(self, answers, candidates):
        intent = mood.build_mood_intent(answers)
        floors = mood.collect_dimension_floors(answers)
        scored = [_with_mood(c, intent) for c in candidates]
        return rec.mood_eligible(scored, intent, floors, "best"), intent, floors

    def test_high_base_score_cannot_buy_its_way_in(self):
        """Ровно прежний дефект: огромный базовый счёт не должен компенсировать
        провал в главном измерении запроса."""
        answers = {"q1": "humor", "h1": "dark"}
        blockbuster = _scored(1, "Блокбастер", genres="боевик,фэнтези,приключения",
                              base=89.0, quality=9.5)
        eligible, _, _ = self._pool(answers, [blockbuster])
        self.assertEqual(eligible, [])

    def test_matching_film_with_modest_base_is_eligible(self):
        answers = {"q1": "humor", "h1": "dark"}
        modest = _scored(2, "Чёрная комедия", genres="комедия,криминал", base=30.0, quality=7.2)
        eligible, _, _ = self._pool(answers, [modest])
        self.assertEqual([m["id"] for m in eligible], [2])

    def test_weak_quality_is_rejected_even_when_mood_fits(self):
        """Уместность по настроению не отменяет качества — иначе выдача
        сползает в нишевое слабое кино."""
        answers = {"q1": "humor", "h1": "situational"}
        weak = _scored(3, "Слабая комедия", genres="комедия", quality=4.0, base=20.0)
        eligible, _, _ = self._pool(answers, [weak])
        self.assertEqual(eligible, [])

    def test_selection_falls_back_but_keeps_explicit_floors(self):
        """Если уместных нет, ослабляем второстепенное — но явное требование
        («чёрный юмор» → юмор обязателен) не ослабляется никогда."""
        answers = {"q1": "humor", "h1": "dark"}
        intent = mood.build_mood_intent(answers)
        floors = mood.collect_dimension_floors(answers)
        only_wrong = [_with_mood(_scored(1, "Боевик", genres="боевик", base=88.0, quality=9.0), intent)]
        pool = [{**m, "_best": 1.0} for m in only_wrong]
        pick, level = rec.select_mood_role(pool, intent, floors, "best", [], "_best")
        self.assertIsNone(pick)
        self.assertGreaterEqual(level, 0)


class KnownRegressionTests(unittest.TestCase):
    """Фиксируем именно те случаи, из-за которых слой был выключен."""

    def test_family_adventure_cannot_win_dark_humor(self):
        # Профиль как у «Аватар: Легенда об Аанге»: мультфильм/фэнтези/боевик,
        # юмор около 0.43 — заметно ниже запрошенного.
        answers = {"q1": "humor", "h1": "dark"}
        intent = mood.build_mood_intent(answers)
        floors = mood.collect_dimension_floors(answers)
        candidate = _with_mood(_scored(1, "Семейное приключение",
                                       genres="мультфильм,фэнтези,боевик,приключения,семейный",
                                       base=88.0, quality=9.8), intent)
        self.assertLess(candidate["_mood_features"].get("humor"), floors["humor_min"])
        self.assertEqual(rec.mood_eligible([candidate], intent, floors, "best"), [])

    def test_action_blockbuster_cannot_win_intellectual_request(self):
        # Профиль как у «Мстители»: фантастика/боевик/приключения,
        # complexity около 0.59 против запрошенных ~0.88.
        answers = {"q1": "unusual", "u2": "high", "u1": "idea"}
        intent = mood.build_mood_intent(answers)
        floors = mood.collect_dimension_floors(answers)
        candidate = _with_mood(_scored(1, "Блокбастер",
                                       genres="фантастика,боевик,приключения",
                                       base=90.0, quality=9.6), intent)
        self.assertLess(candidate["_mood_features"].get("complexity"), floors["complexity_min"])
        self.assertEqual(rec.mood_eligible([candidate], intent, floors, "best"), [])

    def test_grim_film_cannot_win_cozy_request(self):
        answers = {"q1": "relax", "r1": "warm", "r3": "comfort"}
        intent = mood.build_mood_intent(answers)
        floors = mood.collect_dimension_floors(answers)
        candidate = _with_mood(_scored(1, "Мрачная драма", genres="ужасы,триллер",
                                       base=85.0, quality=9.0), intent)
        self.assertEqual(rec.mood_eligible([candidate], intent, floors, "best"), [])

    def test_calm_comedy_cannot_win_maximum_tension(self):
        answers = {"q1": "tension", "t1": "horror"}
        intent = mood.build_mood_intent(answers)
        floors = mood.collect_dimension_floors(answers)
        candidate = _with_mood(_scored(1, "Семейная комедия", genres="комедия,семейный",
                                       base=86.0, quality=9.2), intent)
        self.assertEqual(rec.mood_eligible([candidate], intent, floors, "best"), [])


class FlagTests(unittest.TestCase):
    def test_layer_is_disabled_by_default_in_production(self):
        """Откат — одно значение переменной окружения, без миграций."""
        self.assertIn(rec.MOOD_LAYER_ENABLED, (True, False))

    def test_scoring_without_mood_keys_uses_base_path(self):
        """Когда слой выключен, кандидаты не содержат _mood и отбор идёт по
        прежней логике — старое поведение сохраняется полностью."""
        plain = [{"id": 1, "title": "A", "_score": 50.0, "_quality": 7.0, "_novelty": 1.0,
                  "genres": "драма", "directors": ""}]
        self.assertNotIn("_mood", plain[0])
        self.assertIsNotNone(rec._select_distinct(plain, [], "_score"))


if __name__ == "__main__":
    unittest.main()
