import unittest
from urllib.parse import parse_qs, urlparse

from fastapi import Request
from fastapi.responses import Response

import main
from main import _HTML_CSP, index, log_slow_requests


def _request(path: str) -> Request:
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 443),
    })


class HttpSecurityHeadersTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_api_responses_are_not_cacheable_or_shareable(self):
        async def call_next(_request):
            return Response("{}", media_type="application/json")

        response = await log_slow_requests(_request("/api/me"), call_next)

        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertIn("X-Init-Data", response.headers["vary"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["referrer-policy"], "strict-origin-when-cross-origin")

    async def test_index_has_restrictive_content_security_policy(self):
        response = await index()

        self.assertEqual(response.headers["content-security-policy"], _HTML_CSP)
        self.assertIn("object-src 'none'", response.headers["content-security-policy"])
        self.assertNotIn("script-src 'self' https://telegram.org 'unsafe-inline'",
                         response.headers["content-security-policy"])

    def test_avatar_capability_is_bound_to_viewer_subject_and_expiry(self):
        old_token = main.BOT_TOKEN
        try:
            main.BOT_TOKEN = "audit-token"
            url = main._avatar_url(101, 202)
            params = parse_qs(urlparse(url).query)
            expiry = int(params["exp"][0])

            self.assertEqual(params["viewer"], ["101"])
            self.assertEqual(params["sig"], [main._avatar_signature(101, 202, expiry)])
            self.assertNotEqual(params["sig"], [main._avatar_signature(102, 202, expiry)])
            self.assertNotEqual(params["sig"], [main._avatar_signature(101, 203, expiry)])
        finally:
            main.BOT_TOKEN = old_token
