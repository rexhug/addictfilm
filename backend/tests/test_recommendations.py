import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db
import main
import reasons
import recommendations
from recommendation_questions import next_question_id, public_question


async def _keep_local_catalog(_user_id, _partner_id, _weights, candidates, _minimum):
    """Test helper: recommendation rules, not external providers, are under test."""
    return candidates


class RecommendationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})
        await db.upsert_user({"id": 2, "first_name": "Two", "username": "two"})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def _film(self, suffix: int, title: str, *, genres="Drama", rating="8.0", votes="10000", plot="", runtime="110 min"):
        return await db.get_or_create_film(
            f"tt9000{suffix:03d}", title, "2022", genres, runtime, rating, votes, plot,
            f"https://images.example/{suffix}.jpg", actors="Actor One", directors="Director One",
        )

    def test_question_graph_is_eight_steps_and_never_leaks_weights(self):
        answers = {}
        route = []
        while (question_id := next_question_id(answers)) is not None:
            route.append(question_id)
            # Choose the first option for this structural route check.
            answers[question_id] = public_question(question_id, "ru")["options"][0]["id"]
        self.assertEqual(route, ["q1", "r1", "r2", "r3", "c1", "c2", "c3", "c4"])
        payload = public_question("q1", "ru")
        self.assertEqual(payload["id"], "q1")
        self.assertNotIn("weights", str(payload))
        self.assertNotIn("filters", str(payload))

    async def test_candidate_pool_excludes_watched_rejected_and_recently_shown(self):
        allowed = await self._film(1, "Allowed")
        watched = await self._film(2, "Watched")
        rejected = await self._film(3, "Rejected")
        shown = await self._film(4, "Shown")
        await db.set_status(1, watched, "watched")
        await db.record_recommendation_history(1, rejected, "quiz", action="rejected")
        await db.record_recommendation_history(1, shown, "random", action="shown")

        candidates = await db.get_recommendation_candidates(1, limit=60)
        self.assertEqual({item["id"] for item in candidates}, {allowed})

    async def test_pair_pool_excludes_a_film_watched_by_either_person(self):
        shared_candidate = await self._film(11, "Shared candidate")
        watched_by_partner = await self._film(12, "Partner watched")
        await db.set_status(2, watched_by_partner, "watched")

        candidates = await db.get_recommendation_candidates(1, partner_id=2, limit=60)
        self.assertEqual({item["id"] for item in candidates}, {shared_candidate})

    async def test_quiz_returns_three_distinct_explainable_roles(self):
        await self._film(21, "Mystery A", genres="Thriller, Crime", rating="9.0", plot="A crime mystery")
        await self._film(22, "Mystery B", genres="Drama, Mystery", rating="8.5", plot="A mystery")
        await self._film(23, "Mystery C", genres="Crime", rating="8.1", plot="A detective crime")
        answers = {
            "q1": "tension", "t1": "mystery", "t2_mystery": "crime", "t4": "clear",
            "c1": "any", "c2": "any", "c3": "safe", "c4": "solo",
        }
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            items = await recommendations.quiz_results(1, answers, "ru")
        self.assertEqual([item["role"] for item in items], ["best", "reliable", "unexpected"])
        self.assertEqual(len({item["id"] for item in items}), 3)
        # Причины — стабильные коды, а не пересказ анкеты и не внутренние теги.
        self.assertTrue(all(item["reasons"] for item in items))
        self.assertTrue(all(set(item["reasons"]) <= reasons.KNOWN_CODES for item in items))
        self.assertTrue(all(item["explanation"] for item in items))
        self.assertTrue(all("_tags" not in item for item in items))

    def test_public_movie_keeps_provider_rating_separate_from_quality_score(self):
        """Votes can help ranking, but must never inflate the UI rating."""
        movie = {"id": 9, "title": "Rated", "imdb_rating": "7.2", "kp_rating": "7.0", "imdb_votes": "1000000"}
        self.assertGreater(recommendations._quality(movie), 7.2)
        self.assertEqual(
            recommendations.public_movie(movie, role="best", codes=[reasons.HIGH_QUALITY],
                                         language="ru")["rating"],
            7.2,
        )

    async def test_random_pick_uses_local_quality_pool(self):
        await self._film(31, "Low", rating="5.5")
        high = await self._film(32, "High", rating="8.6")
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            item = await recommendations.random_recommendation(1, "ru")
        self.assertIsNotNone(item)
        self.assertEqual(item["id"], high)
        self.assertEqual(item["role"], "random")

    async def test_random_pick_recycles_history_without_repeating_previous(self):
        first = await self._film(33, "First", rating="8.4")
        second = await self._film(34, "Second", rating="6.0")
        await db.record_recommendation_history(1, second, "random", action="shown")
        await db.record_recommendation_history(1, first, "random", action="shown")
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            item = await recommendations.random_recommendation(1, "ru")
        self.assertIsNotNone(item)
        self.assertEqual(item["id"], second)
        self.assertEqual(item["availability_tier"], "any_eligible")
        self.assertEqual(item["strategy"], "available")

    async def test_cooldown_relaxation_never_revives_watched_or_rejected(self):
        watched = await self._film(35, "Watched", rating="9.5")
        rejected = await self._film(36, "Rejected", rating="9.4")
        allowed = await self._film(37, "Allowed", rating="6.1")
        await db.set_status(1, watched, "watched")
        await db.record_recommendation_history(1, rejected, "random", action="rejected")
        await db.record_recommendation_history(1, allowed, "random", action="shown")
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            item = await recommendations.random_recommendation(1, "ru")
        self.assertEqual(item["id"], allowed)

    async def test_last_resort_can_use_an_artless_film_and_repeat_it(self):
        film_id = await db.get_or_create_film(
            "tt9000038", "Only eligible", "2022", "Drama", "110 min",
            "2.8", "1000000", "Story", None, actors="Actor", directors="Director",
            media_type="movie",
        )
        await db.record_recommendation_history(1, film_id, "random", action="shown")
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            item = await recommendations.random_recommendation(1, "ru")
        self.assertEqual(item["id"], film_id)
        self.assertEqual(item["availability_tier"], "any_eligible")
        self.assertEqual(item["strategy"], "available")

    async def test_pair_cooldown_uses_both_people_history(self):
        shown_by_partner = await self._film(39, "Partner just saw this", rating="9.2")
        alternative = await self._film(40, "Shared alternative", rating="8.2")
        await db.record_recommendation_history(2, shown_by_partner, "random", action="shown")
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            item = await recommendations.random_recommendation(1, "ru", partner_id=2)
        self.assertEqual(item["id"], alternative)

    async def test_quiz_does_not_relax_an_explicit_runtime_limit(self):
        await self._film(41, "Too long", rating="9.0", runtime="180 min")
        answers = {
            "q1": "relax", "r1": "warm", "r2": "medium", "r3": "comfort",
            "c1": "short", "c2": "any", "c3": "safe", "c4": "solo",
        }
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            self.assertEqual(await recommendations.quiz_results(1, answers, "ru"), [])

    async def test_quiz_api_keeps_state_server_side_and_removes_pair_when_unavailable(self):
        user = {"id": 1}
        started = await main.recommendation_quiz_start(main.RecommendationStartBody(language="ru"), user)
        self.assertEqual(started["question"]["id"], "q1")
        session_id = started["id"]
        answered = await main.recommendation_quiz_answer(
            session_id,
            main.RecommendationAnswerBody(language="ru", question_id="q1", answer_id="relax"),
            user,
        )
        self.assertEqual(answered["question"]["id"], "r1")

        # Advance to the context question. The pair option must not be rendered
        # for an account with no active partner, even though it exists in the
        # canonical server graph.
        data = answered
        while data["question"]["id"] != "c4":
            question = data["question"]
            data = await main.recommendation_quiz_answer(
                session_id,
                main.RecommendationAnswerBody(language="ru", question_id=question["id"], answer_id=question["options"][0]["id"]),
                user,
            )
        self.assertNotIn("pair", {option["id"] for option in data["question"]["options"]})

    async def test_completed_session_persists_the_same_result_cards(self):
        session = await db.create_recommendation_session(1, "v1", "stable-session", None)
        await db.update_recommendation_session(1, session["id"], {"q1": "relax"}, None, "complete")
        cards = [{"id": 42, "title": "Stable choice", "role": "best"}]
        await db.save_recommendation_session_results(1, session["id"], cards)
        restored = await db.get_recommendation_session(1, session["id"])
        self.assertEqual(restored["results"], cards)
