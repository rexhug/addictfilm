"""Качество подбора: доверие к оценкам, личные вкусы, честность к паре и разнообразие.

Проверяем именно те дефекты, ради которых движок переделывался, а не просто
«функция что-то вернула».
"""
import unittest

import recommendations as rec


def _film(film_id, title, *, rating="8.0", votes="1000", genres="драма",
          directors="", plot="", wishlist=False):
    return {"id": film_id, "title": title, "imdb_rating": rating, "imdb_votes": votes,
            "genres": genres, "directors": directors, "actors": "", "plot": plot,
            "runtime": "110 min", "year": "2015", "in_wishlist": wishlist}


class BayesianQualityTests(unittest.TestCase):
    def test_ten_out_of_ten_with_two_votes_loses_to_proven_film(self):
        """Главный прежний дефект: 10.0 по двум голосам обгоняла 8.2 по сотням тысяч."""
        hyped = _film(1, "Две оценки", rating="10.0", votes="2")
        proven = _film(2, "Проверенный", rating="8.2", votes="450000")
        self.assertLess(rec._quality(hyped), rec._quality(proven))

    def test_many_votes_keep_rating_almost_intact(self):
        adjusted = rec.bayesian_rating(8.2, 450000)
        self.assertAlmostEqual(adjusted, 8.2, delta=0.05)

    def test_few_votes_are_pulled_towards_catalog_mean(self):
        adjusted = rec.bayesian_rating(10.0, 2)
        self.assertLess(adjusted, 7.0)
        self.assertGreater(adjusted, rec._GLOBAL_MEAN_RATING)

    def test_film_without_rating_has_zero_quality(self):
        self.assertEqual(rec._quality(_film(3, "Без оценки", rating="0", votes="0")), 0.0)


class PreferenceSignalTests(unittest.TestCase):
    def test_disliked_genre_gets_negative_affinity(self):
        # Пользователь стабильно низко оценивает ужасы.
        preferences = {"genres": {"ужасы": -3.0, "драма": 4.0}, "people": {}, "count": 40}
        horror = _film(1, "Ужастик", genres="ужасы")
        drama = _film(2, "Драма", genres="драма")
        self.assertLess(rec._genre_affinity(horror, preferences), 0)
        self.assertGreater(rec._genre_affinity(drama, preferences), 0)

    def test_disliked_genre_lowers_final_score(self):
        preferences = {"genres": {"ужасы": -4.0}, "people": {}, "count": 40}
        horror = _film(1, "Ужастик", genres="ужасы")
        neutral = _film(2, "Нейтральный", genres="приключения")
        scored_horror = rec.score_film(horror, {}, preferences)
        scored_neutral = rec.score_film(neutral, {}, preferences)
        self.assertLess(scored_horror["_score"], scored_neutral["_score"])

    def test_unknown_people_do_not_create_affinity(self):
        preferences = {"genres": {}, "people": {"дэвид финчер": 5.0}, "count": 30}
        stranger = _film(1, "Чужой режиссёр", directors="Кто-то Другой")
        self.assertEqual(rec._people_affinity(stranger, preferences), 0.0)


class ColdStartTests(unittest.TestCase):
    def test_confidence_grows_with_history(self):
        self.assertEqual(rec.profile_confidence({"count": 0}), 0.0)
        self.assertLess(rec.profile_confidence({"count": 5}), 0.5)
        self.assertEqual(rec.profile_confidence({"count": 100}), 1.0)

    def test_new_user_scores_lean_on_quality_not_taste(self):
        """У новичка личный вклад обнуляется — иначе движок «угадывает» вкусы
        на пустом месте."""
        cold = {"genres": {"драма": 5.0}, "people": {}, "count": 0}
        warm = {"genres": {"драма": 5.0}, "people": {}, "count": 100}
        film = _film(1, "Драма", genres="драма")
        self.assertEqual(rec.score_film(film, {}, cold)["_affinity"], 0.0)
        self.assertGreater(rec.score_film(film, {}, warm)["_affinity"], 0.0)

    def test_cold_start_still_prefers_stronger_film(self):
        cold = {"genres": {}, "people": {}, "count": 0}
        good = rec.score_film(_film(1, "Хороший", rating="8.4", votes="200000"), {}, cold)
        weak = rec.score_film(_film(2, "Слабый", rating="5.4", votes="200000"), {}, cold)
        self.assertGreater(good["_score"], weak["_score"])


class PairFairnessTests(unittest.TestCase):
    def test_strong_disagreement_scores_below_mutual_liking(self):
        # Один в восторге, второй категорически против.
        conflict = rec.pair_fairness(100.0, 10.0)
        # Оба относятся ровно и положительно.
        agreement = rec.pair_fairness(58.0, 58.0)
        self.assertLess(conflict, agreement)

    def test_minimum_matters_more_than_average(self):
        # Одинаковое среднее, разный минимум — выигрывает более согласованный.
        self.assertLess(rec.pair_fairness(90.0, 10.0), rec.pair_fairness(55.0, 45.0))

    def test_fairness_is_between_min_and_average(self):
        value = rec.pair_fairness(80.0, 40.0)
        self.assertGreaterEqual(value, 40.0)
        self.assertLessEqual(value, 60.0)


class DiversityTests(unittest.TestCase):
    def test_similarity_detects_same_genre_and_director(self):
        a = _film(1, "A", genres="триллер,криминал", directors="Дэвид Финчер")
        b = _film(2, "B", genres="триллер,криминал", directors="Дэвид Финчер")
        c = _film(3, "C", genres="комедия", directors="Кто-то")
        self.assertGreater(rec.film_similarity(a, b), rec.film_similarity(a, c))

    def test_mmr_avoids_a_near_duplicate_second_pick(self):
        """Второй вариант не должен быть тем же жанром и режиссёром, если есть
        сопоставимая по силе альтернатива."""
        first = {**_film(1, "Триллер один", genres="триллер", directors="Финчер"), "_s": 100.0}
        twin = {**_film(2, "Триллер два", genres="триллер", directors="Финчер"), "_s": 98.0}
        other = {**_film(3, "Комедия", genres="комедия", directors="Другой"), "_s": 95.0}
        ranked = [first, twin, other]
        best = rec._select_distinct(ranked, [], "_s")
        second = rec._select_distinct(ranked, [best], "_s")
        self.assertEqual(best["id"], 1)
        self.assertEqual(second["id"], 3)

    def test_selection_never_repeats_a_film(self):
        films = [{**_film(i, f"Фильм {i}", genres="драма"), "_s": 100.0 - i} for i in range(1, 5)]
        chosen = []
        for _ in range(3):
            pick = rec._select_distinct(films, chosen, "_s")
            self.assertIsNotNone(pick)
            chosen.append(pick)
        self.assertEqual(len({item["id"] for item in chosen}), 3)

    def test_selection_returns_none_when_pool_exhausted(self):
        films = [{**_film(1, "Единственный"), "_s": 10.0}]
        only = rec._select_distinct(films, [], "_s")
        self.assertIsNone(rec._select_distinct(films, [only], "_s"))


if __name__ == "__main__":
    unittest.main()
