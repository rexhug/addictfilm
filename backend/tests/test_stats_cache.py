import unittest

import stats_cache


class StatsCacheTests(unittest.TestCase):
    def setUp(self):
        stats_cache.clear()

    def test_returns_copy_and_clears_after_mutation(self):
        value = {"year": {"count": 3}}
        stats_cache.put(("personal", 7, 2026), value)
        cached = stats_cache.get(("personal", 7, 2026))
        cached["year"]["count"] = 99
        self.assertEqual(stats_cache.get(("personal", 7, 2026))["year"]["count"], 3)
        stats_cache.clear()
        self.assertIsNone(stats_cache.get(("personal", 7, 2026)))
