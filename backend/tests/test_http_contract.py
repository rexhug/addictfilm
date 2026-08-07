"""End-to-end through ASGI: everything above the function level was untested.

The suite calls handlers directly, so a whole class of bugs could not be seen:
a route registered after the catch-all static mount, a lost Depends, a changed
status code, a missing security header. None of those live inside a handler.
"""
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from fastapi.routing import APIRoute

BOT_TOKEN = "test:token"
os.environ.setdefault("BOT_TOKEN", BOT_TOKEN)


def signed_init_data(user: dict, *, auth_date: int | None = None) -> str:
    """Same construction as test_auth: a real signature, not a stubbed check."""
    data = {"auth_date": str(auth_date or int(time.time())), "query_id": "http-contract",
            "user": json.dumps(user)}
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class HttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import database as db
        cls.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(cls.temp.name) / "http.db")
        db.DATABASE_URL, db._PG = "", False
        import deps
        import main
        from fastapi.testclient import TestClient
        cls.db = db
        cls.main = main
        deps.BOT_TOKEN = BOT_TOKEN
        # Real lifespan: init_db runs, so the schema under test is the real one.
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.temp.cleanup()

    def setUp(self):
        import ratelimit
        import user_touch
        ratelimit._reset_for_tests()
        user_touch._reset_for_tests()

    def _auth(self, user_id: int = 4242):
        return {"X-Init-Data": signed_init_data({"id": user_id, "first_name": "Denys"})}

    def test_healthz_is_public(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_api_requires_valid_init_data(self):
        for path in ("/api/me", "/api/movies", "/api/stats", "/api/settings"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_forged_init_data_is_rejected(self):
        response = self.client.get(
            "/api/me", headers={"X-Init-Data": "user=%7B%22id%22%3A1%7D&hash=deadbeef"})
        self.assertEqual(response.status_code, 401)

    def test_a_valid_signature_reaches_the_handler(self):
        response = self.client.get("/api/me", headers=self._auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], 4242)

    def test_index_sends_csp_and_is_not_cached(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors https://*.telegram.org",
                      response.headers["content-security-policy"])
        self.assertIn("no-store", response.headers["cache-control"])

    def test_admin_routes_are_registered_before_the_static_mount(self):
        # A catch-all StaticFiles mount added too early would answer 404 here
        # and the whole admin surface would vanish silently.
        response = self.client.get("/api/admin/collections", headers=self._auth())
        self.assertNotEqual(response.status_code, 404)

    def test_admin_surface_denies_an_ordinary_user_rather_than_404(self):
        """403, not 404: the difference proves the route exists and the gate ran."""
        response = self.client.get("/api/admin/analytics", headers=self._auth())
        self.assertEqual(response.status_code, 403)

    def test_write_endpoints_answer_429_once_the_budget_is_spent(self):
        import ratelimit
        headers = self._auth(user_id=4343)
        for _ in range(ratelimit.MUTATION_MAX):
            ratelimit.allow_mutation(4343)
        response = self.client.post("/api/movie/999999/status", headers=headers,
                                    json={"status": "want_to_watch"})
        self.assertEqual(response.status_code, 429)

    def test_unknown_api_path_is_not_swallowed_by_the_static_mount(self):
        self.assertEqual(self.client.get("/api/definitely-not-a-route").status_code, 404)

    def test_movie_detail_never_presents_a_raw_backdrop_url_as_a_hero(self):
        """Карточка обязана отдавать ту же медиа-модель, что рулетка и подбор.

        Пока она возвращала сырую строку каталога, непроверенный backdrop_url
        был для фронтенда истиной — и заглушка провайдера уезжала на экран как
        настоящий кадр. Синтетический id намеренно: он отключает фоновое
        обогащение, и тест не ходит наружу.
        """
        import asyncio
        film_id = asyncio.run(self.db.get_or_create_film(
            imdb_id="kp_777001", title="Фильм с плохим кадром", genres="драма",
            media_type="movie", poster_url="https://p/ok.jpg", year="2025",
            backdrop_url="https://avatars.mds.yandex.net/get-ott/plate/1344x756"))
        body = self.client.get(f"/api/movie/{film_id}", headers=self._auth()).json()
        self.assertEqual(body["hero_type"], "poster_blur")
        self.assertEqual(body["hero_url"], "https://p/ok.jpg")
        self.assertNotEqual(body["hero_url"], body["backdrop_url"])
        # Поля карточки нормализация не съедает.
        for field in ("status", "my_rating", "my_comment", "cast", "partner",
                      "community", "share_link"):
            self.assertIn(field, body)


def _registered_api_routes(routes):
    """Yield routes through FastAPI's included-router wrappers.

    Newer FastAPI releases keep included routers nested until OpenAPI is built.
    Walking that stable public shape lets this test catch duplicate HTTP
    registrations that an ``include_in_schema=False`` legacy route would hide.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        nested_router = getattr(route, "original_router", None)
        if nested_router is not None:
            yield from _registered_api_routes(nested_router.routes)


class RouteRegistrationTests(unittest.TestCase):
    def test_each_path_and_http_method_has_one_owner(self):
        """Router extraction must not leave a hidden legacy route behind."""
        import main

        owners: dict[tuple[str, str], list[str]] = {}
        for route in _registered_api_routes(main.app.router.routes):
            for method in route.methods or ():
                key = (route.path, method)
                owners.setdefault(key, []).append(route.name)
        duplicates = {key: names for key, names in owners.items() if len(names) > 1}
        self.assertEqual(duplicates, {})


ADMIN_PATHS = {
    ("/api/admin/analytics", "GET"),
    ("/api/admin/audit-log", "GET"),
    ("/api/admin/backfill-actor-photos", "POST"),
    ("/api/admin/backfill-director-photos", "POST"),
    ("/api/admin/backfill-posters", "POST"),
    ("/api/admin/collections", "GET"),
    ("/api/admin/collections", "POST"),
    ("/api/admin/collections/featured/order", "PUT"),
    ("/api/admin/collections/{collection_id}", "DELETE"),
    ("/api/admin/collections/{collection_id}", "GET"),
    ("/api/admin/collections/{collection_id}", "PATCH"),
    ("/api/admin/collections/{collection_id}/archive", "POST"),
    ("/api/admin/collections/{collection_id}/films", "POST"),
    ("/api/admin/collections/{collection_id}/films/{film_id}", "DELETE"),
    ("/api/admin/collections/{collection_id}/items/order", "PUT"),
    ("/api/admin/collections/{collection_id}/publish", "POST"),
    ("/api/admin/collections/{collection_id}/restore", "POST"),
    ("/api/admin/collections/{collection_id}/unpublish", "POST"),
    ("/api/admin/enrich-film-people/{imdb_id}", "POST"),
    ("/api/admin/enrichment", "GET"),
    ("/api/admin/enrichment/exceptions", "GET"),
    ("/api/admin/enrichment/{film_id}/override", "DELETE"),
    ("/api/admin/enrichment/{film_id}/override", "PUT"),
    ("/api/admin/enrichment/{film_id}/rebuild", "POST"),
    ("/api/admin/films/resolve", "POST"),
    ("/api/admin/films/{film_id}/hero-presentation", "PATCH"),
    ("/api/admin/films/{film_id}/movie-flow", "PATCH"),
    ("/api/admin/films/{film_id}/poster-display", "PATCH"),
    ("/api/admin/performance", "GET"),
    ("/api/admin/reviews/{review_id}/hide", "PATCH"),
    ("/api/admin/upgrade-omdb-posters", "POST"),
}


class AdminRouteTableTests(unittest.TestCase):
    """Moving the admin surface into its own module must not lose a path.

    A separate check on purpose: the admin tests call handlers directly and
    would pass even if the route were never registered with the application.
    """

    @classmethod
    def setUpClass(cls):
        # Own client, not the one HttpContractTests owns: sharing it made the
        # lifespan context be entered twice and closed twice. Registration and
        # a 401 are both decided before any handler touches the database, so
        # this client deliberately does not start the lifespan.
        import main
        from fastapi.testclient import TestClient
        cls.main = main
        cls.client = TestClient(main.app)

    def test_every_admin_route_stays_registered(self):
        # app.routes stopped flattening included routers in this FastAPI
        # version, so the schema is what actually reflects registration.
        spec = self.main.app.openapi()
        registered = {(path, method.upper())
                      for path, operations in spec["paths"].items()
                      for method in operations}
        self.assertTrue(registered >= ADMIN_PATHS, ADMIN_PATHS - registered)

    def test_admin_routes_are_not_swallowed_by_the_static_mount(self):
        # Registering the router after app.mount("/") would answer 404 here.
        self.assertNotEqual(self.client.get("/api/admin/collections").status_code, 404)

    def test_admin_routes_reject_anonymous_callers(self):
        for path in ("/api/admin/collections", "/api/admin/audit-log", "/api/admin/analytics"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)


GUARDS = {"require_editor", "require_admin_user", "require_admin"}


def _guards_of(route) -> set[str]:
    """Every dependency name reachable from a route, at any depth.

    Walks the tree rather than the top level: a guard declared as
    dependencies=[Depends(...)] and one taken as a handler argument end up at
    different depths, and both must count.
    """
    seen, stack = set(), list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            seen.add(getattr(dep.call, "__name__", ""))
        stack.extend(dep.dependencies)
    return seen


class AdminGuardTests(unittest.TestCase):
    def test_every_admin_route_keeps_an_authorization_guard(self):
        """The blind spot in comparing OpenAPI: Depends never reaches the schema.

        A route that lost its guard while being moved into the router would
        produce a byte-identical schema and an admin surface open to anyone.
        Nothing else in the suite would notice.
        """
        from routers.admin import router
        for route in router.routes:
            with self.subTest(path=route.path):
                self.assertTrue(GUARDS & _guards_of(route), f"{route.path} без гейта")

    def test_the_guard_distribution_matches_what_each_surface_needs(self):
        """Not just "some guard": the wrong one is its own failure. Analytics on
        require_editor would hand audience numbers to a collections editor.
        """
        from routers.admin import router
        # Counted per route, not per path: /collections carries GET and POST,
        # and each method is guarded separately.
        by_guard = {}
        for route in router.routes:
            for guard in GUARDS & _guards_of(route):
                by_guard.setdefault(guard, []).append(route.path)
        self.assertEqual(set(by_guard["require_admin_user"]), {"/api/admin/analytics"})
        # Token-gated maintenance: curl and scripts, never the Mini App.
        self.assertEqual(set(by_guard["require_admin"]), {
            "/api/admin/performance",
            "/api/admin/backfill-posters",
            "/api/admin/backfill-actor-photos",
            "/api/admin/backfill-director-photos",
            "/api/admin/upgrade-omdb-posters",
            "/api/admin/enrich-film-people/{imdb_id}",
        })
        self.assertEqual(len(by_guard["require_editor"]), 24)
        self.assertEqual(sum(len(paths) for paths in by_guard.values()), len(router.routes))


if __name__ == "__main__":
    unittest.main()
