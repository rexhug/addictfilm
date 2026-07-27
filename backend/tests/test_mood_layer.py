"""Слой настроения: признаки фильмов, намерение из анкеты и близость.

Главное, что здесь проверяется: измерение, про которое человек ничего не
говорил, не влияет на совпадение, а плохо размеченный фильм не получает
крайних оценок.
"""
import unittest

import mood
import recommendations as rec


def _film(film_id=1, *, genres="комедия", plot="", runtime="110 min", title="Фильм"):
    return {"id": film_id, "title": title, "genres": genres, "plot": plot,
            "runtime": runtime, "imdb_rating": "7.5", "imdb_votes": "50000"}


class FeatureGenerationTests(unittest.TestCase):
    def test_all_values_stay_in_range(self):
        for genres in ("комедия", "ужасы,триллер,драма", "", "неизвестный жанр"):
            features = mood.build_movie_features(_film(genres=genres, plot="месть и смерть, война"))
            for dimension, value in features.values.items():
                with self.subTest(genres=genres, dimension=dimension):
                    self.assertGreaterEqual(value, 0.0)
                    self.assertLessEqual(value, 1.0)

    def test_unknown_genre_falls_back_to_neutral(self):
        features = mood.build_movie_features(_film(genres="что-то незнакомое"))
        self.assertEqual(set(features.values.values()), {0.5})
        self.assertLess(features.confidence, 0.3)

    def test_comedy_has_high_humor_and_low_tension(self):
        features = mood.build_movie_features(_film(genres="комедия"))
        self.assertGreater(features.get("humor"), 0.8)
        self.assertLess(features.get("tension"), 0.3)

    def test_horror_has_high_tension_and_darkness(self):
        features = mood.build_movie_features(_film(genres="ужасы"))
        self.assertGreater(features.get("tension"), 0.85)
        self.assertGreater(features.get("darkness"), 0.85)

    def test_signature_genre_survives_mixing(self):
        """Комедия в наборе жанров не должна растворяться до «нет юмора»."""
        mixed = mood.build_movie_features(_film(genres="мультфильм,комедия,приключения,фэнтези"))
        self.assertGreater(mixed.get("humor"), 0.45)

    def test_tone_is_averaged_not_pulled_up(self):
        """А тон усредняется: семейный мультфильм не становится мрачным из-за
        одной военной линии."""
        family = mood.build_movie_features(_film(genres="мультфильм,семейный,военный"))
        self.assertLess(family.get("darkness"), 0.7)

    def test_markers_match_on_word_boundary_only(self):
        """Подстроки ловили «во-семь» как семью и «в-мест-е» как месть."""
        false_positive = _film(genres="комедия", plot="Промотав восемь жизней, он поехал вместе с друзьями")
        markers = mood.detect_markers(false_positive)
        self.assertNotIn("family_bond", markers)
        self.assertNotIn("revenge", markers)
        real = _film(genres="драма", plot="Его семья погибла, и он поклялся отомстить")
        real_markers = mood.detect_markers(real)
        self.assertIn("family_bond", real_markers)
        self.assertIn("revenge", real_markers)

    def test_unknown_markers_are_ignored(self):
        features = mood.build_movie_features(_film(genres="драма", plot="Совершенно нейтральное описание"))
        baseline = mood.build_movie_features(_film(genres="драма", plot=""))
        self.assertEqual(features.values["darkness"], baseline.values["darkness"])

    def test_marker_deltas_are_capped(self):
        """Длинное описание с десятком маркеров не уводит измерение в край."""
        loaded = _film(genres="драма", plot=(
            "смерть гибель утрата месть возмездие война фронт битва "
            "выживание катастрофа психолог антиутопия призрак демон"))
        features = mood.build_movie_features(loaded)
        baseline = mood.build_movie_features(_film(genres="драма", plot=""))
        shift = features.get("darkness") - baseline.get("darkness")
        self.assertLessEqual(shift, mood.MAX_TOTAL_MARKER_DELTA + 1e-6)

    def test_features_are_deterministic(self):
        film = _film(genres="триллер", plot="Расследование убийства")
        first, second = mood.build_movie_features(film), mood.build_movie_features(film)
        self.assertEqual(first.values, second.values)
        self.assertEqual(first.source_hash, second.source_hash)

    def test_source_hash_changes_with_content(self):
        a = mood.build_movie_features(_film(genres="драма"))
        b = mood.build_movie_features(_film(genres="комедия"))
        self.assertNotEqual(a.source_hash, b.source_hash)

    def test_confidence_is_bounded_and_grows_with_metadata(self):
        poor = mood.build_movie_features({"id": 1, "title": "X", "genres": "", "plot": "", "runtime": ""})
        rich = mood.build_movie_features(_film(genres="драма,триллер", plot="Расследование, месть, война"))
        self.assertGreaterEqual(poor.confidence, 0.0)
        self.assertLessEqual(rich.confidence, 1.0)
        self.assertGreater(rich.confidence, poor.confidence)


class MoodIntentTests(unittest.TestCase):
    def test_unanswered_dimensions_are_absent(self):
        intent = mood.build_mood_intent({"q1": "humor"})
        self.assertIn("humor", intent.targets)
        # Про темп и сложность человек ничего не сказал — их вообще нет.
        self.assertNotIn("pace", intent.targets)
        self.assertNotIn("complexity", intent.targets)

    def test_targets_are_averaged_by_importance(self):
        intent = mood.build_mood_intent({"q1": "relax", "r2": "slow"})
        self.assertIn("pace", intent.targets)
        self.assertLess(intent.targets["pace"], 0.4)   # оба ответа тянут к спокойному

    def test_duration_becomes_hard_constraint_not_mood(self):
        intent = mood.build_mood_intent({"q1": "humor", "c1": "short"})
        self.assertEqual(intent.hard_constraints.get("runtime_max"), 100)
        self.assertNotIn("runtime", intent.targets)

    def test_unknown_answers_are_ignored(self):
        intent = mood.build_mood_intent({"q1": "humor", "zz": "nonsense", "q1_extra": "???"})
        self.assertIn("humor", intent.targets)

    def test_empty_answers_produce_empty_intent(self):
        intent = mood.build_mood_intent({})
        self.assertEqual(intent.targets, {})

    def test_all_values_validate(self):
        for answer in ("relax", "tension", "humor", "emotion", "unusual"):
            intent = mood.build_mood_intent({"q1": answer})
            intent.validate()   # не должно бросать
            for value in list(intent.targets.values()) + list(intent.importance.values()):
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_invalid_dimension_is_rejected(self):
        with self.assertRaises(ValueError):
            mood.MoodIntent(targets={"nonsense": 0.5}, importance={"nonsense": 1.0}).validate()

    def test_out_of_range_target_is_rejected(self):
        with self.assertRaises(ValueError):
            mood.MoodIntent(targets={"humor": 1.7}, importance={"humor": 1.0}).validate()


class SimilarityTests(unittest.TestCase):
    def _features(self, **values):
        base = {dim: 0.5 for dim in mood.MOOD_DIMENSIONS}
        base.update(values)
        return mood.MovieMoodFeatures(film_id=1, feature_version="test", values=base, confidence=1.0)

    def test_exact_match_is_one(self):
        intent = mood.MoodIntent(targets={"humor": 0.9}, importance={"humor": 1.0})
        self.assertAlmostEqual(mood.mood_similarity(intent, self._features(humor=0.9)), 1.0, places=5)

    def test_opposite_match_is_zero(self):
        intent = mood.MoodIntent(targets={"humor": 1.0}, importance={"humor": 1.0})
        self.assertAlmostEqual(mood.mood_similarity(intent, self._features(humor=0.0)), 0.0, places=5)

    def test_unspecified_dimensions_do_not_affect_score(self):
        """Главная защита: несовпадение по темпу не портит запрос про юмор."""
        intent = mood.MoodIntent(targets={"humor": 0.9}, importance={"humor": 1.0})
        matching_humor = self._features(humor=0.9, pace=0.0, tension=1.0)
        self.assertAlmostEqual(mood.mood_similarity(intent, matching_humor), 1.0, places=5)

    def test_zero_importance_dimension_is_skipped(self):
        intent = mood.MoodIntent(targets={"humor": 0.9, "pace": 0.1}, importance={"humor": 1.0, "pace": 0.0})
        self.assertAlmostEqual(mood.mood_similarity(intent, self._features(humor=0.9, pace=1.0)), 1.0, places=5)

    def test_empty_intent_is_neutral(self):
        self.assertEqual(mood.mood_similarity(mood.MoodIntent(), self._features()), 0.5)

    def test_low_confidence_pulls_towards_neutral(self):
        self.assertAlmostEqual(mood.confidence_adjusted(1.0, 0.0), 0.5, places=5)
        self.assertAlmostEqual(mood.confidence_adjusted(1.0, 1.0), 1.0, places=5)
        self.assertGreater(mood.confidence_adjusted(1.0, 0.5), 0.5)

    def test_output_always_bounded(self):
        for similarity in (-1.0, 0.0, 0.5, 1.0, 2.0):
            for confidence in (-1.0, 0.0, 0.5, 1.0, 2.0):
                value = mood.confidence_adjusted(similarity, confidence)
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)


class IntegrationTests(unittest.TestCase):
    def test_cold_start_leans_on_mood_more_than_history(self):
        self.assertGreater(mood.resolve_mood_weight(0.0), mood.resolve_mood_weight(1.0))
        self.assertAlmostEqual(mood.resolve_mood_weight(0.0), 0.55, places=2)
        self.assertAlmostEqual(mood.resolve_mood_weight(1.0), 0.38, places=2)

    def test_combined_score_is_bounded(self):
        for base in (0.0, 0.5, 1.0):
            for mood_score in (0.0, 0.5, 1.0):
                value = mood.combine_scores(base_score=base, mood_score=mood_score, profile_confidence=0.5)
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_mood_layer_keeps_base_order_when_questionnaire_is_silent(self):
        ranked = [{**_film(1, genres="драма"), "_score": 90.0},
                  {**_film(2, genres="комедия"), "_score": 50.0}]
        result = rec.apply_mood_layer(ranked, {}, preferences_count=10)
        self.assertEqual([m["id"] for m in result], [1, 2])

    def test_mood_layer_promotes_a_matching_film(self):
        """Человек попросил посмеяться — комедия должна обойти драму, даже если
        базовый счёт у неё чуть ниже."""
        ranked = [{**_film(1, genres="драма", title="Драма"), "_score": 92.0},
                  {**_film(2, genres="комедия", title="Комедия"), "_score": 86.0}]
        result = rec.apply_mood_layer(ranked, {"q1": "humor"}, preferences_count=0)
        self.assertEqual(result[0]["id"], 2)

    def test_mood_layer_reports_its_components(self):
        ranked = [{**_film(1, genres="комедия"), "_score": 80.0},
                  {**_film(2, genres="ужасы"), "_score": 70.0}]
        result = rec.apply_mood_layer(ranked, {"q1": "humor"}, preferences_count=5)
        for movie in result:
            self.assertIn("_mood", movie)
            self.assertIn("_base_score", movie)
            self.assertGreaterEqual(movie["_mood"], 0.0)
            self.assertLessEqual(movie["_mood"], 1.0)


if __name__ == "__main__":
    unittest.main()
