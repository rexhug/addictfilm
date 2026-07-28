"""Умный случайный выбор: явные стратегии вместо лотереи по топ-80.

Прежняя реализация разыгрывала первые 80 кандидатов по базовому счёту: одни и
те же крепкие фильмы выпадали снова и снова, а остальной каталог не имел шансов.
"""
import collections
import random
import unittest

import reasons
import smart_random as sr


def _movie(film_id, *, quality=7.5, affinity=0.0, novelty=2.0):
    return {"id": film_id, "title": f"Фильм {film_id}", "_quality": quality,
            "_affinity": affinity, "_novelty": novelty, "_score": 50.0}


class StrategyWeightTests(unittest.TestCase):
    def test_established_profile_keeps_all_three_strategies(self):
        self.assertGreater(sr.resolve_strategy_weights(sr.DEFAULT_POLICY, 0.9)[sr.TASTE_MATCH], 0)

    def test_cold_start_redistributes_taste_match(self):
        """Обещать совпадение со вкусом, ничего о вкусе не зная, — обман."""
        weights = sr.resolve_strategy_weights(sr.DEFAULT_POLICY, 0.0)
        self.assertEqual(weights[sr.TASTE_MATCH], 0.0)
        self.assertGreater(weights[sr.RELIABLE], sr.DEFAULT_POLICY.strategy_weights[sr.RELIABLE])
        self.assertGreater(weights[sr.DISCOVERY], sr.DEFAULT_POLICY.strategy_weights[sr.DISCOVERY])
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)

    def test_strategy_distribution_follows_configuration(self):
        rng = random.Random(20260727)
        weights = sr.resolve_strategy_weights(sr.DEFAULT_POLICY, 0.9)
        counts = collections.Counter(sr.choose_strategy(weights, rng=rng) for _ in range(6000))
        for name, expected in sr.DEFAULT_POLICY.strategy_weights.items():
            with self.subTest(strategy=name):
                self.assertAlmostEqual(counts[name] / 6000, expected, delta=0.03)


class PoolTests(unittest.TestCase):
    def test_reliable_excludes_negative_affinity(self):
        pool = sr.build_pool(sr.RELIABLE, [_movie(1, quality=9.0, affinity=-0.5),
                                           _movie(2, quality=9.0, affinity=0.2)],
                             sr.DEFAULT_POLICY, 0.9)
        self.assertEqual([m["id"] for m in pool], [2])

    def test_reliable_requires_the_highest_quality_floor(self):
        self.assertEqual(sr.build_pool(sr.RELIABLE, [_movie(1, quality=6.5)],
                                       sr.DEFAULT_POLICY, 0.9), [])

    def test_taste_match_is_empty_without_a_confident_profile(self):
        movie = _movie(1, quality=8.0, affinity=0.5)
        self.assertEqual(sr.build_pool(sr.TASTE_MATCH, [movie], sr.DEFAULT_POLICY, 0.1), [])
        self.assertTrue(sr.build_pool(sr.TASTE_MATCH, [movie], sr.DEFAULT_POLICY, 0.9))

    def test_discovery_rejects_the_same_blockbusters(self):
        pool = sr.build_pool(sr.DISCOVERY, [_movie(1, quality=8.5, novelty=0.2),
                                            _movie(2, quality=7.0, novelty=3.5)],
                             sr.DEFAULT_POLICY, 0.5)
        self.assertEqual([m["id"] for m in pool], [2])

    def test_discovery_still_respects_a_quality_floor(self):
        self.assertEqual(sr.build_pool(sr.DISCOVERY, [_movie(1, quality=4.0, novelty=4.5)],
                                       sr.DEFAULT_POLICY, 0.5), [])

    def test_pool_is_not_limited_to_the_first_eighty(self):
        """Прежний дефект: всё за пределами топ-80 не имело шансов вообще."""
        ranked = [_movie(index, quality=7.5, novelty=3.0) for index in range(200)]
        self.assertGreater(len(sr.build_pool(sr.RELIABLE, ranked, sr.DEFAULT_POLICY, 0.9)), 80)


class SelectionTests(unittest.TestCase):
    def test_empty_catalog_returns_nothing(self):
        movie, strategy, fallback = sr.select([], profile_confidence=0.5)
        self.assertIsNone(movie)
        self.assertEqual(strategy, sr.AVAILABLE)
        self.assertTrue(fallback)

    def test_fallback_is_used_when_the_chosen_bucket_is_empty(self):
        movie, strategy, fallback = sr.select([_movie(1, quality=6.5, novelty=4.0)],
                                              profile_confidence=0.0, rng=random.Random(7))
        self.assertEqual(movie["id"], 1)
        self.assertEqual(strategy, sr.DISCOVERY)
        self.assertTrue(fallback)

    def test_cold_start_never_selects_taste_match(self):
        rng = random.Random(1)
        ranked = [_movie(index, quality=8.0, affinity=0.6, novelty=3.0) for index in range(30)]
        for _ in range(200):
            self.assertNotEqual(sr.select(ranked, profile_confidence=0.0, rng=rng)[1],
                                sr.TASTE_MATCH)

    def test_selection_spreads_across_the_catalog(self):
        rng = random.Random(3)
        ranked = [_movie(index, quality=7.6, novelty=3.0) for index in range(40)]
        picked = {sr.select(ranked, profile_confidence=0.0, rng=rng)[0]["id"] for _ in range(120)}
        self.assertGreater(len(picked), 15)

    def test_unqualified_fallback_is_never_labelled_reliable(self):
        ranked = [_movie(1, quality=4.0, affinity=-1.0, novelty=0.0)]
        movie, strategy, _ = sr.select(ranked, profile_confidence=0.0)
        self.assertIsNone(movie)
        self.assertEqual(strategy, sr.AVAILABLE)
        movie, strategy, fallback = sr.select(
            ranked, profile_confidence=0.0, allow_unqualified=True)
        self.assertEqual(movie["id"], 1)
        self.assertEqual(strategy, sr.AVAILABLE)
        self.assertTrue(fallback)


class ReasonTests(unittest.TestCase):
    def test_every_strategy_has_its_own_reason_code(self):
        codes = {sr.STRATEGY_REASONS[n] for n in (
            sr.RELIABLE, sr.TASTE_MATCH, sr.DISCOVERY, sr.AVAILABLE)}
        self.assertEqual(len(codes), 4)
        self.assertTrue(codes <= reasons.KNOWN_CODES)

    def test_high_quality_is_not_claimed_without_evidence(self):
        self.assertNotIn(reasons.HIGH_QUALITY,
                         sr.reason_codes_for(sr.RELIABLE, _movie(1, quality=7.0), pair=False))
        self.assertIn(reasons.HIGH_QUALITY,
                      sr.reason_codes_for(sr.RELIABLE, _movie(1, quality=8.4), pair=False))

    def test_taste_match_reason_requires_positive_affinity(self):
        self.assertNotIn(reasons.MATCHES_USER_TASTE,
                         sr.reason_codes_for(sr.TASTE_MATCH, _movie(1, affinity=0.0), pair=False))

    def test_pair_reason_only_in_pair_mode(self):
        self.assertNotIn(reasons.PAIR_FRIENDLY,
                         sr.reason_codes_for(sr.RELIABLE, _movie(1), pair=False))
        self.assertIn(reasons.PAIR_FRIENDLY,
                      sr.reason_codes_for(sr.RELIABLE, _movie(1), pair=True))

    def test_codes_are_known(self):
        for strategy in (sr.RELIABLE, sr.TASTE_MATCH, sr.DISCOVERY, sr.AVAILABLE):
            with self.subTest(strategy=strategy):
                codes = sr.reason_codes_for(strategy, _movie(1, quality=8.5, affinity=0.4,
                                                             novelty=4.0), pair=True)
                self.assertTrue(set(codes) <= reasons.KNOWN_CODES)

    def test_policy_is_versioned(self):
        self.assertTrue(sr.DEFAULT_POLICY.version.startswith("smart-random-"))


if __name__ == "__main__":
    unittest.main()
