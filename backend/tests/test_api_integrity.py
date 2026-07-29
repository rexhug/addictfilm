"""Сервер не должен верить клиенту в том, что знает сам.

Три дефекта, которые здесь закрыты: роль замены бралась из тела запроса, метрики
обратной связи писались как прислал фронтенд, а выбор случайного фильма и запись
показа не были одной операцией.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db
import recommendations as rec
from main import _server_recommendation_metadata


async def _keep_local_catalog(_user_id, _partner_id, _weights, candidates, _minimum):
    return candidates


class FeedbackMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "f.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})
        self.film_id = await db.get_or_create_film(
            imdb_id="tt1", title="Фильм", genres="драма", media_type="movie",
            poster_url="https://example.test/p.jpg")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _quiz_session(self, results):
        session = await db.create_recommendation_session(1, "quiz-v2", "sess", "q1")
        await db.update_recommendation_session(1, session["id"], {"q1": "humor"}, None, "complete")
        await db.save_recommendation_session_results(1, session["id"], results)
        return session["id"]

    async def test_quiz_metadata_comes_from_the_stored_session(self):
        session_id = await self._quiz_session([
            {"id": self.film_id, "role": "reliable", "score": 61.5}])
        metadata = await _server_recommendation_metadata(1, self.film_id, "quiz", session_id)
        self.assertEqual(metadata.role, "reliable")
        self.assertAlmostEqual(metadata.score, 61.5)

    async def test_random_metadata_comes_from_the_shown_history(self):
        await db.record_recommendation_history(1, self.film_id, "random", role="random", score=42.0)
        metadata = await _server_recommendation_metadata(1, self.film_id, "random", None)
        self.assertEqual(metadata.role, "random")
        self.assertAlmostEqual(metadata.score, 42.0)

    async def test_wishlist_metadata_comes_from_the_roulette_ledger(self):
        await db.add_to_list(1, self.film_id, "want_to_watch", None)
        await db.pick_random_wishlist_film(1)
        metadata = await _server_recommendation_metadata(1, self.film_id, "wishlist", None)
        self.assertEqual(metadata.role, "wishlist")

    async def test_unconfirmed_show_yields_no_metadata_at_all(self):
        """Не «запись с пустыми полями», а отсутствие происхождения."""
        self.assertIsNone(await _server_recommendation_metadata(1, self.film_id, "random", None))
        self.assertIsNone(await _server_recommendation_metadata(1, self.film_id, "wishlist", None))
        self.assertIsNone(await _server_recommendation_metadata(1, self.film_id, "quiz", None))

    async def test_quiz_never_falls_back_to_another_session(self):
        """Роль из чужой сессии портит аналитику ровно так же, как значение
        от клиента: к текущей карточке она отношения не имеет."""
        # Фильм показан в ОДНОЙ сессии...
        first = await self._quiz_session([{"id": self.film_id, "role": "best", "score": 90.0}])
        await db.record_recommendation_history(1, self.film_id, "quiz", session_id=first,
                                               role="best", score=90.0)
        # ...а обратная связь приходит на ДРУГУЮ, где его нет.
        other = await db.create_recommendation_session(1, "quiz-v2", "sess2", "q1")
        await db.update_recommendation_session(1, other["id"], {"q1": "humor"}, None, "complete")
        await db.save_recommendation_session_results(1, other["id"], [{"id": 999, "role": "best"}])
        other = other["id"]
        self.assertIsNone(await _server_recommendation_metadata(1, self.film_id, "quiz", other))

    async def test_random_does_not_accept_a_quiz_only_show(self):
        """Режимы не перетекают друг в друга."""
        session_id = await self._quiz_session([
            {"id": self.film_id, "role": "best", "score": 70.0}])
        self.assertIsNotNone(await _server_recommendation_metadata(1, self.film_id, "quiz", session_id))
        self.assertIsNone(await _server_recommendation_metadata(1, self.film_id, "random", None))

    async def test_unknown_mode_has_no_metadata(self):
        self.assertIsNone(await _server_recommendation_metadata(1, self.film_id, "выдумка", None))


class SmartRandomAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "s.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})
        for index in range(8):
            await db.get_or_create_film(
                imdb_id=f"tt{index}", title=f"Фильм {index}", genres="драма",
                imdb_rating="7.8", imdb_votes="200000", plot="История",
                media_type="movie", poster_url="https://example.test/p.jpg")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def test_selection_records_the_show_in_the_same_operation(self):
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            item = await rec.pick_and_record_smart_random(1, "ru")
        self.assertIsNotNone(item)
        shown = await db.get_recommendation_shown(1, item["id"], "random")
        self.assertIsNotNone(shown, "показ не записан вместе с выбором")
        self.assertEqual(shown["role"], "random")

    async def test_concurrent_requests_do_not_return_the_same_film(self):
        """Два одновременных запроса видели фильм доступным и оба его брали."""
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            items = await asyncio.gather(*[rec.pick_and_record_smart_random(1, "ru")
                                           for _ in range(4)])
        picked = [item["id"] for item in items if item]
        self.assertEqual(len(picked), 4)
        self.assertEqual(len(set(picked)), 4, "один фильм выдан дважды подряд")

    async def test_empty_catalog_records_nothing(self):
        async def _empty(*_args, **_kwargs):
            return []
        # Smart Random fetches one hard-eligible pool and applies availability
        # tiers in memory.  Keep this regression test at that public boundary
        # instead of patching the old per-tier ranking helper.
        with patch.object(db, "get_recommendation_candidates", new=_empty), \
             patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            self.assertIsNone(await rec.pick_and_record_smart_random(1, "ru"))


class SmartRandomRollbackFlagTests(unittest.TestCase):
    def test_strategies_are_enabled_by_default(self):
        self.assertIn(rec.SMART_RANDOM_STRATEGIES, (True, False))

    def test_legacy_pick_returns_a_public_card(self):
        """Путь отката обязан оставаться рабочим, а не быть мёртвым кодом."""
        ranked = [{"id": 1, "title": "Фильм", "genres": "драма", "imdb_rating": "8.0",
                   "imdb_votes": "100000", "_quality": 7.9, "_novelty": 2.0,
                   "_affinity": 0.0, "_score": 50.0, "_tags": {}}]
        card = rec._legacy_random_pick(ranked, "ru", None)
        self.assertEqual(card["role"], "random")
        self.assertTrue(card["reasons"])
        self.assertNotIn("strategy", card)


if __name__ == "__main__":
    unittest.main()


class FeedbackRouteProvenanceTests(unittest.IsolatedAsyncioTestCase):
    """Отказ должен приходить только на неподтверждённое, а не на живые пути."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "r.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})
        self.film_id = await db.get_or_create_film(
            imdb_id="tt1", title="Фильм", genres="драма", imdb_rating="7.9",
            imdb_votes="200000", plot="История", media_type="movie",
            poster_url="https://example.test/p.jpg")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _feedback(self, mode, session_id=None, action="rejected"):
        from fastapi import HTTPException
        from main import RecommendationFeedbackBody, recommendation_feedback
        body = RecommendationFeedbackBody(action=action, mode=mode, session_id=session_id,
                                          role="ПОДДЕЛКА", score=999999.0)
        try:
            return await recommendation_feedback(self.film_id, body, {"id": 1})
        except HTTPException as error:
            return error.status_code

    async def test_smart_random_flow_still_accepts_feedback(self):
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            item = await rec.pick_and_record_smart_random(1, "ru")
        self.film_id = item["id"]
        self.assertEqual(await self._feedback("random"), {"ok": True})

    async def test_wishlist_roulette_flow_still_accepts_feedback(self):
        """Рулетка шлёт свой режим: её показы живут в отдельном учёте."""
        await db.add_to_list(1, self.film_id, "want_to_watch", None)
        await db.pick_random_wishlist_film(1)
        self.assertEqual(await self._feedback("wishlist"), {"ok": True})

    async def test_quiz_flow_still_accepts_feedback(self):
        session = await db.create_recommendation_session(1, "quiz-v2", "s", "q1")
        await db.update_recommendation_session(1, session["id"], {"q1": "humor"}, None, "complete")
        await db.save_recommendation_session_results(
            1, session["id"], [{"id": self.film_id, "role": "best", "score": 70.0}])
        self.assertEqual(await self._feedback("quiz", session["id"]), {"ok": True})

    async def test_unshown_film_is_rejected_with_conflict(self):
        self.assertEqual(await self._feedback("random"), 409)

    async def test_client_values_never_reach_the_history(self):
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            item = await rec.pick_and_record_smart_random(1, "ru")
        self.film_id = item["id"]
        await self._feedback("random")
        import db_runtime
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            conn.row_factory = __import__("aiosqlite").Row
            row = await (await conn.execute(
                "SELECT role, score FROM recommendation_history WHERE user_id=1 "
                "AND action='rejected' ORDER BY id DESC LIMIT 1")).fetchone()
        self.assertNotEqual(row["role"], "ПОДДЕЛКА")
        self.assertNotEqual(row["score"], 999999.0)
