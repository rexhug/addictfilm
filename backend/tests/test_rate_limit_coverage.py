"""Write paths must have a ceiling of their own.

Throttling existed only on search, the image proxy and review writes. Everything
that mutates a list, a rating or a quiz session was unbounded — and quiz calls
INSERT a row each, which made them the cheapest way for one account to write to
Postgres in a loop.
"""
import unittest

import main
import ratelimit
from fastapi import HTTPException
from fastapi.routing import APIRoute


def _registered_api_routes(routes):
    """Walk FastAPI's included-router wrappers as well as root routes."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        nested_router = getattr(route, "original_router", None)
        if nested_router is not None:
            yield from _registered_api_routes(nested_router.routes)


class MutationBudgetTests(unittest.TestCase):
    def setUp(self):
        ratelimit._reset_for_tests()

    def tearDown(self):
        ratelimit._reset_for_tests()

    def test_a_user_is_cut_off_after_the_configured_number_of_writes(self):
        for _ in range(ratelimit.MUTATION_MAX):
            self.assertTrue(ratelimit.allow_mutation(1))
        self.assertFalse(ratelimit.allow_mutation(1))

    def test_the_budget_is_per_user(self):
        for _ in range(ratelimit.MUTATION_MAX):
            ratelimit.allow_mutation(1)
        self.assertFalse(ratelimit.allow_mutation(1))
        self.assertTrue(ratelimit.allow_mutation(2))

    def test_the_window_frees_up_again(self):
        for _ in range(ratelimit.MUTATION_MAX):
            ratelimit.allow_mutation(1)
        self.assertFalse(ratelimit.allow_mutation(1))
        ratelimit._reset_for_tests()
        self.assertTrue(ratelimit.allow_mutation(1))

    def test_quiz_and_mutation_budgets_are_independent(self):
        """Otherwise finishing a quiz would block adding a movie, and one noisy
        path would silently spend another path's quota."""
        for _ in range(ratelimit.QUIZ_MAX):
            self.assertTrue(ratelimit.allow_quiz(7))
        self.assertFalse(ratelimit.allow_quiz(7))
        self.assertTrue(ratelimit.allow_mutation(7))

    def test_search_quota_is_untouched_by_writes(self):
        for _ in range(ratelimit.MUTATION_MAX):
            ratelimit.allow_mutation(9)
        self.assertTrue(ratelimit.allow_user(9))


class ThrottleDependencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ratelimit._reset_for_tests()

    def tearDown(self):
        ratelimit._reset_for_tests()

    async def test_the_dependency_answers_429_rather_than_letting_the_write_through(self):
        user = {"id": 42}
        for _ in range(ratelimit.MUTATION_MAX):
            await main.throttled_mutation(user=user)
        with self.assertRaises(HTTPException) as ctx:
            await main.throttled_mutation(user=user)
        self.assertEqual(ctx.exception.status_code, 429)

    async def test_the_quiz_dependency_has_its_own_ceiling(self):
        user = {"id": 43}
        for _ in range(ratelimit.QUIZ_MAX):
            await main.throttled_quiz(user=user)
        with self.assertRaises(HTTPException) as ctx:
            await main.throttled_quiz(user=user)
        self.assertEqual(ctx.exception.status_code, 429)


class ThrottleWiringTests(unittest.TestCase):
    """A guard that exists but is not attached protects nothing."""

    def test_write_endpoints_depend_on_a_throttle(self):
        expected = {
            ("/api/add", "POST"): "throttled_mutation",
            ("/api/movie/{film_id}/rate", "POST"): "throttled_mutation",
            ("/api/movie/{film_id}/status", "POST"): "throttled_mutation",
            ("/api/movie/{film_id}", "DELETE"): "throttled_mutation",
            ("/api/partner/invite", "POST"): "throttled_mutation",
            ("/api/recommendations/quiz/start", "POST"): "throttled_quiz",
            ("/api/recommendations/quiz/{session_id}/answer", "POST"): "throttled_quiz",
            ("/api/recommendations/random", "POST"): "throttled_quiz",
            ("/api/wishlist/random", "POST"): "throttled_quiz",
        }
        routes = tuple(_registered_api_routes(main.app.router.routes))
        for (path, method), guard in expected.items():
            with self.subTest(path=path, method=method):
                route = next(r for r in routes
                             if getattr(r, "path", None) == path
                             and method in getattr(r, "methods", ()))
                names = {sub.call.__name__ for sub in route.dependant.dependencies}
                self.assertIn(guard, names)


if __name__ == "__main__":
    unittest.main()
