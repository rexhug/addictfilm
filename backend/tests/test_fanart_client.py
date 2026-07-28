"""Клиент Fanart.tv. Ни один тест не ходит в сеть: сессия подставная.

Отдельная тема здесь — ключи. Они не должны попадать ни в URL, ни в текст
исключения, ни в лог: это единственная защита от того, чтобы секрет уехал в
Sentry вместе с обычным таймаутом.
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from unittest import mock

import aiohttp
import fanart


class _FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, error=None):
        self.status = status
        self.headers = headers or {}
        self._payload = payload
        self._error = error

    async def __aenter__(self):
        if self._error:
            raise self._error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)


class _FakeSession:
    """Записывает КАЖДЫЙ запрос: заголовки проверяются, а не предполагаются."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None):
        self.calls.append((url, dict(headers or {})))
        return self._responses.pop(0) if self._responses else _FakeResponse(404)


def _background(**overrides) -> dict:
    return {"id": "1", "url": "https://assets.fanart.tv/fanart/movies/1/a.jpg",
            "lang": "00", "likes": "12", "width": "1920", "height": "1080", **overrides}


def _run(coro):
    return asyncio.run(coro)


class FanartHeaderTests(unittest.TestCase):
    def test_both_keys_are_sent_in_their_own_headers(self):
        session = _FakeSession([_FakeResponse(payload={"moviebackground": []})])
        with mock.patch.object(fanart, "FANART_PROJECT_KEY", "project"), \
             mock.patch.object(fanart, "FANART_CLIENT_KEY", "client"):
            _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        _url, headers = session.calls[0]
        self.assertEqual(headers["api-key"], "project")
        self.assertEqual(headers["client-key"], "client")
        self.assertEqual(headers["accept"], "application/json")

    def test_project_key_alone_is_enough(self):
        session = _FakeSession([_FakeResponse(payload={"moviebackground": []})])
        with mock.patch.object(fanart, "FANART_PROJECT_KEY", "project"), \
             mock.patch.object(fanart, "FANART_CLIENT_KEY", ""):
            _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        _url, headers = session.calls[0]
        self.assertEqual(headers["api-key"], "project")
        self.assertNotIn("client-key", headers)

    def test_personal_key_alone_is_enough(self):
        session = _FakeSession([_FakeResponse(payload={"moviebackground": []})])
        with mock.patch.object(fanart, "FANART_PROJECT_KEY", ""), \
             mock.patch.object(fanart, "FANART_CLIENT_KEY", "client"):
            _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        _url, headers = session.calls[0]
        self.assertEqual(headers["client-key"], "client")
        self.assertNotIn("api-key", headers)

    def test_without_any_key_no_request_is_made(self):
        session = _FakeSession([])
        with mock.patch.object(fanart, "FANART_PROJECT_KEY", ""), \
             mock.patch.object(fanart, "FANART_CLIENT_KEY", ""):
            self.assertEqual(_run(fanart.get_movie_backgrounds("tt0111161", session=session)), [])
        self.assertEqual(session.calls, [])

    def test_keys_never_appear_in_the_request_url(self):
        session = _FakeSession([_FakeResponse(payload={"moviebackground": []})])
        with mock.patch.object(fanart, "FANART_PROJECT_KEY", "supersecret"), \
             mock.patch.object(fanart, "FANART_CLIENT_KEY", "alsosecret"):
            _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        url, _headers = session.calls[0]
        self.assertNotIn("supersecret", url)
        self.assertNotIn("alsosecret", url)


class FanartIdentifierTests(unittest.TestCase):
    def test_only_a_well_formed_imdb_id_reaches_the_network(self):
        for value in ("", None, "1234567", "tt", "ttabc", "nm0000123", "  "):
            session = _FakeSession([])
            with mock.patch.object(fanart, "FANART_PROJECT_KEY", "project"):
                self.assertEqual(_run(fanart.get_movie_backgrounds(value, session=session)), [])
            self.assertEqual(session.calls, [], value)

    def test_identifier_is_normalized_before_use(self):
        session = _FakeSession([_FakeResponse(payload={"moviebackground": []})])
        with mock.patch.object(fanart, "FANART_PROJECT_KEY", "project"):
            _run(fanart.get_movie_backgrounds("  TT0111161 ", session=session))
        self.assertTrue(session.calls[0][0].endswith("/movies/tt0111161"))


class FanartParsingTests(unittest.TestCase):
    def setUp(self):
        self.keys = mock.patch.object(fanart, "FANART_PROJECT_KEY", "project")
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def test_a_normal_payload_is_parsed(self):
        session = _FakeSession([_FakeResponse(payload={"moviebackground": [_background()]})])
        images = _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        self.assertEqual(len(images), 1)
        self.assertEqual((images[0].width, images[0].height, images[0].likes), (1920, 1080, 12))
        self.assertEqual(images[0].language, "00")

    def test_http_urls_are_upgraded_to_https(self):
        raw = _background(url="http://assets.fanart.tv/fanart/movies/1/a.jpg")
        session = _FakeSession([_FakeResponse(payload={"moviebackground": [raw]})])
        images = _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        self.assertEqual(images[0].url, "https://assets.fanart.tv/fanart/movies/1/a.jpg")

    def test_malformed_entries_are_dropped_without_losing_the_good_ones(self):
        payload = {"moviebackground": [
            {"url": "", "width": "1920", "height": "1080"},          # без ссылки
            {"url": "https://assets.fanart.tv/x.jpg"},                # без размеров
            {"url": "https://assets.fanart.tv/y.jpg", "width": "нет", "height": "1080"},
            "строка вместо объекта",
            _background(id="ok"),
        ]}
        session = _FakeSession([_FakeResponse(payload=payload)])
        images = _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        self.assertEqual([image.id for image in images], ["ok"])

    def test_missing_section_is_an_empty_result_not_a_crash(self):
        session = _FakeSession([_FakeResponse(payload={"movieposter": [_background()]})])
        self.assertEqual(_run(fanart.get_movie_backgrounds("tt0111161", session=session)), [])

    def test_404_means_no_artwork_not_an_outage(self):
        session = _FakeSession([_FakeResponse(status=404)])
        self.assertEqual(_run(fanart.get_movie_backgrounds("tt0111161", session=session)), [])


class FanartFailureTests(unittest.TestCase):
    def setUp(self):
        self.keys = mock.patch.object(fanart, "FANART_PROJECT_KEY", "supersecret")
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def test_rate_limit_is_retried_a_bounded_number_of_times(self):
        session = _FakeSession([_FakeResponse(status=429, headers={"Retry-After": "1"})
                                for _ in range(3)])
        with mock.patch.object(fanart.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
            with self.assertRaises(fanart.FanartUnavailable):
                _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        self.assertEqual(len(session.calls), 3)          # ровно _MAX_ATTEMPTS
        self.assertEqual(sleep.await_count, 2)

    def test_rate_limit_that_clears_returns_the_payload(self):
        session = _FakeSession([
            _FakeResponse(status=429, headers={"Retry-After": "1"}),
            _FakeResponse(payload={"moviebackground": [_background()]}),
        ])
        with mock.patch.object(fanart.asyncio, "sleep", new=mock.AsyncMock()):
            images = _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        self.assertEqual(len(images), 1)

    def test_retry_after_is_clamped(self):
        self.assertEqual(fanart._retry_delay("99999"), fanart._MAX_RETRY_DELAY)
        self.assertEqual(fanart._retry_delay("мусор"), 1.0)
        self.assertEqual(fanart._retry_delay(None), 1.0)

    def test_timeout_becomes_a_controlled_error(self):
        session = _FakeSession([_FakeResponse(error=TimeoutError())])
        with self.assertRaises(fanart.FanartUnavailable):
            _run(fanart.get_movie_backgrounds("tt0111161", session=session))

    def test_rejected_keys_are_reported_without_printing_them(self):
        session = _FakeSession([_FakeResponse(status=403)])
        with self.assertRaises(fanart.FanartUnavailable) as caught:
            _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        message = str(caught.exception)
        self.assertIn("FANART_PROJECT_KEY", message)
        self.assertNotIn("supersecret", message)

    def test_key_value_never_reaches_the_log(self):
        session = _FakeSession([_FakeResponse(error=aiohttp.ClientError("boom"))])
        with self.assertLogs(fanart.logger, level=logging.WARNING) as logs:
            with self.assertRaises(fanart.FanartUnavailable):
                _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        self.assertNotIn("supersecret", "\n".join(logs.output))

    def test_server_error_is_retried_then_surfaced(self):
        session = _FakeSession([_FakeResponse(status=503) for _ in range(3)])
        with mock.patch.object(fanart.asyncio, "sleep", new=mock.AsyncMock()):
            with self.assertRaises(fanart.FanartUnavailable):
                _run(fanart.get_movie_backgrounds("tt0111161", session=session))
        self.assertEqual(len(session.calls), 3)


if __name__ == "__main__":
    unittest.main()
