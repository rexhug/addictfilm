"""Флаг решает версию только при СОЗДАНИИ сессии; дальше решает сама сессия.

Иначе выключение флага перевело бы уже начатую сессию на другой движок посреди
опроса: ответы даны одной семантикой, результат посчитан другой.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db
from recommendation import engines
from recommendation.intent import INTENT_MAPPINGS, KNOWN_FEATURES, build_intent
from recommendation_questions import QUESTIONS


class EngineResolutionTests(unittest.TestCase):
    def test_ordinary_user_stays_on_v1(self):
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ENABLED", False), \
             patch.object(engines, "RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW", True):
            self.assertEqual(engines.resolve_engine_for_new_session(is_admin=False).engine_version,
                             engines.ENGINE_V1)

    def test_admin_preview_gives_v2_only_to_admins(self):
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ENABLED", False), \
             patch.object(engines, "RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW", True):
            self.assertEqual(engines.resolve_engine_for_new_session(is_admin=True).engine_version,
                             engines.ENGINE_V2)

    def test_global_flag_gives_v2_to_everyone(self):
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ENABLED", True):
            self.assertEqual(engines.resolve_engine_for_new_session(is_admin=False).engine_version,
                             engines.ENGINE_V2)

    def test_v2_is_disabled_by_default_in_this_build(self):
        self.assertFalse(engines.RECOMMENDATION_ENGINE_V2_ENABLED)

    def test_null_version_is_always_legacy_v1(self):
        for session in ({}, {"engine_version": None}, {"engine_version": ""}):
            with self.subTest(session=session):
                self.assertEqual(engines.resolve_for_session(session).engine_version,
                                 engines.ENGINE_V1)

    def test_disabling_v2_does_not_move_an_existing_v2_session(self):
        """Главный инвариант: сессия живёт своей версией, а не текущим флагом."""
        session = {"engine_version": engines.ENGINE_V2}
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ENABLED", False), \
             patch.object(engines, "RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW", False):
            self.assertEqual(engines.resolve_for_session(session).engine_version, engines.ENGINE_V2)
            self.assertTrue(engines.uses_v2(session))

    def test_enabling_v2_does_not_move_an_existing_v1_session(self):
        session = {"engine_version": engines.ENGINE_V1}
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ENABLED", True):
            self.assertEqual(engines.resolve_for_session(session).engine_version, engines.ENGINE_V1)

    def test_unknown_engine_is_an_explicit_error_not_a_silent_v1(self):
        """Тихо посчитать чужую версию как V1 — то самое смешивание семантики,
        от которого защищает версионирование. Бывает при откате сборки назад."""
        with self.assertRaises(engines.UnknownRecommendationEngine):
            engines.resolve_for_session({"engine_version": "recommendation-v3"})
        self.assertFalse(engines.is_supported({"engine_version": "recommendation-v3"}))
        self.assertTrue(engines.is_supported({"engine_version": None}))
        self.assertTrue(engines.is_supported({"engine_version": engines.ENGINE_V2}))

    def test_quiz_policy_is_separate_from_smart_random_policy(self):
        """Версия smart-random ничего не говорит о семантике квиза."""
        import smart_random
        self.assertEqual(engines.V1.policy_version, engines.QUIZ_POLICY_V1)
        self.assertEqual(engines.V2.policy_version, engines.QUIZ_POLICY_V2)
        self.assertNotEqual(engines.V1.policy_version, engines.V2.policy_version)
        for spec in engines.REGISTRY.values():
            with self.subTest(engine=spec.engine_version):
                self.assertNotEqual(spec.policy_version, smart_random.SMART_RANDOM_POLICY_VERSION)

    def test_every_spec_carries_a_complete_version_set(self):
        for name, spec in engines.REGISTRY.items():
            with self.subTest(engine=name):
                versions = spec.as_session_versions()
                self.assertEqual(set(versions), {"question_version", "engine_version",
                                                 "policy_version", "taxonomy_version",
                                                 "feature_version"})
                self.assertTrue(all(versions.values()))


class SessionDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "e.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def test_v2_session_keeps_its_engine_after_the_flag_is_turned_off(self):
        await db.create_recommendation_session(1, engines.V2.question_version, "s1", "q1",
                                               versions=engines.V2.as_session_versions())
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ENABLED", False), \
             patch.object(engines, "RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW", False):
            stored = await db.get_recommendation_session(1, "s1")
            self.assertEqual(engines.resolve_for_session(stored).engine_version, engines.ENGINE_V2)

    async def test_restart_moves_the_session_to_the_currently_available_engine(self):
        await db.create_recommendation_session(1, engines.V1.question_version, "s2", "q1",
                                               versions=engines.V1.as_session_versions())
        await db.restart_recommendation_session(1, "s2", "q1",
                                                versions=engines.V2.as_session_versions())
        stored = await db.get_recommendation_session(1, "s2")
        self.assertEqual(engines.resolve_for_session(stored).engine_version, engines.ENGINE_V2)

    async def test_versions_match_against_the_sessions_own_engine(self):
        from main import _session_versions_match
        await db.create_recommendation_session(1, engines.V2.question_version, "s3", "q1",
                                               versions=engines.V2.as_session_versions())
        stored = await db.get_recommendation_session(1, "s3")
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ENABLED", False):
            self.assertTrue(_session_versions_match(stored))


class CanonicalIntentTests(unittest.TestCase):
    def test_every_answer_contributes_to_the_intent(self):
        missing = [f"{question_id}:{option['id']}"
                   for question_id, question in QUESTIONS.items()
                   for option in question["options"]
                   if (question_id, option["id"]) not in INTENT_MAPPINGS]
        self.assertEqual(missing, [])

    def test_all_features_come_from_the_single_taxonomy(self):
        for key, contribution in INTENT_MAPPINGS.items():
            features = (contribution.required_features | contribution.forbidden_features
                        | set(contribution.preferred_features))
            with self.subTest(answer=":".join(key)):
                self.assertTrue(features <= KNOWN_FEATURES,
                                f"вне канонического словаря: {sorted(features - KNOWN_FEATURES)}")

    def test_dark_humor_requires_the_canonical_tone(self):
        intent = build_intent({"q1": "humor", "h1": "dark"})
        self.assertIn("dark_comedy", intent.required_features)
        self.assertIn("child_oriented", intent.forbidden_features)

    def test_forbidden_beats_preferred(self):
        """Прямой запрет сильнее пожелания соседнего ответа."""
        intent = build_intent({"q1": "relax", "r3": "comfort", "u3": "nightmare"})
        self.assertIn("disturbing", intent.forbidden_features)
        self.assertNotIn("disturbing", intent.preferred_features)

    def test_required_and_forbidden_never_collide(self):
        intent = build_intent({"q1": "humor", "h1": "dark", "r3": "comfort"})
        intent.validate()
        self.assertFalse(intent.required_features & intent.forbidden_features)

    def test_hard_constraints_come_from_explicit_answers(self):
        self.assertEqual(build_intent({"c1": "short"}).hard_constraints, {"runtime_max": 100})
        self.assertEqual(build_intent({"c2": "modern"}).hard_constraints, {"year_min": 2018})

    def test_exploration_and_context_are_derived(self):
        intent = build_intent({"c3": "surprise", "c4": "pair"})
        self.assertGreater(intent.exploration_level, 0.8)
        self.assertEqual(intent.watch_context, "pair")

    def test_mood_scale_is_reused_not_duplicated(self):
        """Второй источник шкалы настроения гарантированно разъехался бы."""
        import mood
        answers = {"q1": "tension", "t1": "horror"}
        self.assertEqual(build_intent(answers).mood_targets, mood.build_mood_intent(answers).targets)

    def test_empty_answers_produce_a_valid_neutral_intent(self):
        intent = build_intent({})
        intent.validate()
        self.assertEqual(intent.required_features, frozenset())
        self.assertEqual(intent.exploration_level, 0.5)

    def test_invalid_intent_is_rejected(self):
        from recommendation.intent import RecommendationIntent
        with self.assertRaises(ValueError):
            RecommendationIntent(required_features=frozenset({"comedy"}),
                                 forbidden_features=frozenset({"comedy"})).validate()
        with self.assertRaises(ValueError):
            RecommendationIntent(required_features=frozenset({"не-из-словаря"})).validate()


if __name__ == "__main__":
    unittest.main()


class UnsupportedEngineRouteTests(unittest.IsolatedAsyncioTestCase):
    """Незнакомый движок должен давать понятный отказ, а не 500."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "u.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})
        await db.create_recommendation_session(
            1, "quiz-v2", "future", "q1",
            versions={**engines.V1.as_session_versions(), "engine_version": "recommendation-v3"})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def test_route_helper_converts_it_into_a_clear_conflict(self):
        from fastapi import HTTPException
        from main import _require_supported_engine
        session = await db.get_recommendation_session(1, "future")
        with self.assertRaises(HTTPException) as error:
            _require_supported_engine(session)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail["code"], "SESSION_ENGINE_UNSUPPORTED")

    async def test_known_session_passes_through(self):
        from main import _require_supported_engine
        await db.create_recommendation_session(1, "quiz-v2", "ok", "q1",
                                               versions=engines.V1.as_session_versions())
        session = await db.get_recommendation_session(1, "ok")
        self.assertEqual(_require_supported_engine(session).engine_version, engines.ENGINE_V1)
