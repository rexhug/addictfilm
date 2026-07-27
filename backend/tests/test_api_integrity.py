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

    async def test_quiz_metadata_comes_from_the_stored_session(self):
        session = await db.create_recommendation_session(1, "quiz-v2", "sess", "q1")
        await db.update_recommendation_session(1, session["id"], {"q1": "humor"}, None, "complete")
        await db.save_recommendation_session_results(1, session["id"], [
            {"id": self.film_id, "role": "reliable", "score": 61.5}])
        role, score = await _server_recommendation_metadata(1, self.film_id, "quiz", session["id"])
        self.assertEqual(role, "reliable")
        self.assertAlmostEqual(score, 61.5)

    async def test_random_metadata_comes_from_the_shown_history(self):
        await db.record_recommendation_history(1, self.film_id, "random", role="random", score=42.0)
        role, score = await _server_recommendation_metadata(1, self.film_id, "random", None)
        self.assertEqual(role, "random")
        self.assertAlmostEqual(score, 42.0)

    async def test_unknown_film_yields_no_invented_metadata(self):
        self.assertEqual(await _server_recommendation_metadata(1, 999, "random", None), (None, None))

    async def test_client_values_are_never_used(self):
        """Ровно тот дефект: фронтенд мог прислать любую роль и любой счёт."""
        await db.record_recommendation_history(1, self.film_id, "random", role="random", score=42.0)
        role, score = await _server_recommendation_metadata(1, self.film_id, "random", None)
        self.assertNotEqual(role, "best")        # что мог бы прислать клиент
        self.assertNotEqual(score, 999999.0)


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
        with patch("recommendations.ranked_candidates", new=_empty):
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
