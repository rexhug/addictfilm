"""Сессия помнит, какой семантикой она собрана.

Без этого сессия, начатая до деплоя, доигрывается уже другой логикой: ответы
даны по одной анкете, а замена карточки считается по другой — и человек молча
получает смесь двух движков в одном экране.
"""
import tempfile
import unittest
from pathlib import Path

import database as db
import db_runtime
from main import _current_engine_versions, _session_versions_match

_VERSION_FIELDS = ("engine_version", "policy_version", "taxonomy_version", "feature_version")


class SessionVersionStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "v.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _session(self, session_id="s1", versions=None):
        return await db.create_recommendation_session(
            1, versions.get("question_version", "quiz-v2") if versions else "quiz-v2",
            session_id, "q1", versions=versions or _current_engine_versions())

    async def test_creation_records_every_version(self):
        session = await self._session()
        stored = await db.get_recommendation_session(1, session["id"])
        current = _current_engine_versions()
        self.assertEqual(stored["version"], current["question_version"])
        for field in _VERSION_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(stored[field], current[field])

    async def test_versions_survive_answering(self):
        """Ответы меняются, семантика — нет."""
        session = await self._session()
        await db.update_recommendation_session(1, session["id"], {"q1": "humor"}, "h1")
        stored = await db.get_recommendation_session(1, session["id"])
        for field in _VERSION_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(stored[field], _current_engine_versions()[field])

    async def test_restart_refreshes_versions(self):
        """Перезапуск — новый проход, он должен считаться текущей логикой."""
        await self._session("s2", versions={**_current_engine_versions(),
                                            "engine_version": "recommendation-v0"})
        await db.restart_recommendation_session(1, "s2", "q1", versions=_current_engine_versions())
        stored = await db.get_recommendation_session(1, "s2")
        self.assertEqual(stored["engine_version"], _current_engine_versions()["engine_version"])

    async def test_legacy_session_without_versions_stays_valid(self):
        """Сессии старше версионирования не должны стать сломанными."""
        await self._session("s3")
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "UPDATE recommendation_sessions SET engine_version=NULL, policy_version=NULL, "
                "taxonomy_version=NULL, feature_version=NULL WHERE id='s3'")
            await conn.commit()
        stored = await db.get_recommendation_session(1, "s3")
        self.assertIsNone(stored["engine_version"])
        self.assertTrue(_session_versions_match(stored))


class SessionVersionMatchTests(unittest.TestCase):
    def _session(self, **overrides):
        current = _current_engine_versions()
        session = {"version": current["question_version"],
                   **{field: current[field] for field in _VERSION_FIELDS}}
        session.update(overrides)
        return session

    def test_current_session_matches(self):
        self.assertTrue(_session_versions_match(self._session()))

    def test_unknown_versions_are_treated_as_legacy_and_allowed(self):
        self.assertTrue(_session_versions_match(
            self._session(**{field: None for field in _VERSION_FIELDS}, version=None)))

    def test_any_changed_version_stops_recalculation(self):
        """Для ЗНАКОМОГО движка расхождение любой версии — просто несовпадение."""
        for field in ("version", "policy_version", "taxonomy_version", "feature_version"):
            with self.subTest(field=field):
                self.assertFalse(_session_versions_match(self._session(**{field: "чужая-версия"})))

    def test_unknown_engine_is_an_error_not_a_mismatch(self):
        """Неизвестный движок обрабатывается раньше — _require_supported_engine."""
        from recommendation.engines import UnknownRecommendationEngine
        with self.assertRaises(UnknownRecommendationEngine):
            _session_versions_match(self._session(engine_version="recommendation-v9"))

    def test_engine_versions_are_explicit_strings(self):
        current = _current_engine_versions()
        self.assertEqual(set(current), {"question_version", "engine_version", "policy_version",
                                        "taxonomy_version", "feature_version"})
        for name, value in current.items():
            with self.subTest(version=name):
                self.assertTrue(value and isinstance(value, str))


if __name__ == "__main__":
    unittest.main()
