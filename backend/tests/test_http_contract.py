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


if __name__ == "__main__":
    unittest.main()
