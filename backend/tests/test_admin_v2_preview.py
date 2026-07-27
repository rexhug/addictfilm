"""Значок обкатки V2 приходит только от сервера и только администратору.

Клиент не должен уметь его выпросить: иначе это не отражение того, чем реально
считается подбор, а подделываемый признак.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db
from main import _admin_quiz_engine_meta, _quiz_payload
from recommendation import engines


class AdminPreviewMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "p.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "Admin", "username": "admin"})
        await db.upsert_user({"id": 2, "first_name": "User", "username": "user"})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _session(self, session_id, spec, user_id=1):
        return await db.create_recommendation_session(
            user_id, spec.question_version, session_id, "q1",
            versions=spec.as_session_versions())

    def _as_admin(self):
        return patch("main._effective_role", return_value="admin")

    def _as_user(self):
        return patch("main._effective_role", return_value=None)

    async def test_admin_v2_session_payload_exposes_preview_metadata(self):
        session = await self._session("a1", engines.V2)
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ENABLED", False), \
             patch.object(engines, "RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW", True), \
             self._as_admin():
            payload = await _quiz_payload(session, "ru", 1)
        self.assertEqual(payload["engine_version"], engines.ENGINE_V2)
        self.assertIs(payload["engine_preview"], True)

    async def test_regular_user_does_not_receive_preview_metadata(self):
        session = await self._session("u1", engines.V1, user_id=2)
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW", True), self._as_user():
            payload = await _quiz_payload(session, "ru", 2)
        self.assertEqual(session["engine_version"], engines.ENGINE_V1)
        self.assertNotIn("engine_preview", payload)
        self.assertNotIn("engine_version", payload)

    async def test_admin_on_a_v1_session_sees_no_preview_flag(self):
        """Значок отражает движок СЕССИИ, а не право администратора."""
        session = await self._session("a2", engines.V1)
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW", True), self._as_admin():
            payload = await _quiz_payload(session, "ru", 1)
        self.assertEqual(payload["engine_version"], engines.ENGINE_V1)
        self.assertIs(payload["engine_preview"], False)

    async def test_existing_v1_session_remains_v1_after_admin_preview_enabled(self):
        await self._session("keep-v1", engines.V1)
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW", True):
            stored = await db.get_recommendation_session(1, "keep-v1")
            self.assertEqual(engines.resolve_for_session(stored).engine_version, engines.ENGINE_V1)

    async def test_existing_v2_session_remains_v2_after_preview_disabled(self):
        await self._session("keep-v2", engines.V2)
        with patch.object(engines, "RECOMMENDATION_ENGINE_V2_ENABLED", False), \
             patch.object(engines, "RECOMMENDATION_ENGINE_V2_ADMIN_PREVIEW", False):
            stored = await db.get_recommendation_session(1, "keep-v2")
            self.assertEqual(engines.resolve_for_session(stored).engine_version, engines.ENGINE_V2)

    async def test_metadata_is_identical_for_cached_and_fresh_results(self):
        """Иначе значок появлялся бы только при первом открытии результатов."""
        session = await self._session("res", engines.V2)
        with self._as_admin():
            first = await _admin_quiz_engine_meta(session, 1)
            await db.save_recommendation_session_results(1, "res", [{"id": 1, "role": "best"}])
            cached = await db.get_recommendation_session(1, "res")
            second = await _admin_quiz_engine_meta(cached, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["engine_version"], engines.ENGINE_V2)

    async def test_editor_role_also_gets_preview(self):
        session = await self._session("ed", engines.V2)
        with patch("main._effective_role", return_value="editor"):
            meta = await _admin_quiz_engine_meta(session, 1)
        self.assertIs(meta["engine_preview"], True)

    async def test_unsupported_engine_is_still_reported_as_conflict(self):
        """Метаданные не должны обходить проверку поддерживаемости движка."""
        from fastapi import HTTPException
        await db.create_recommendation_session(
            1, "quiz-v2", "future", "q1",
            versions={**engines.V1.as_session_versions(), "engine_version": "recommendation-v9"})
        session = await db.get_recommendation_session(1, "future")
        with self._as_admin(), self.assertRaises(HTTPException) as error:
            await _admin_quiz_engine_meta(session, 1)
        self.assertEqual(error.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
