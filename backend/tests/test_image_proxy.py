import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import HTTPException

import main
import ratelimit
from main import _MAX_IMAGE_BYTES, _is_allowed_image_url, _read_image_limited


class FakeImageStream:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class ImageProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ratelimit._reset_for_tests()

    def test_accepts_only_configured_image_hosts(self):
        self.assertTrue(_is_allowed_image_url("https://st.kp.yandex.net/images/actor.jpg"))
        self.assertTrue(_is_allowed_image_url("https://avatars.mds.yandex.net/get-kinopoisk-image/a/360"))
        self.assertTrue(_is_allowed_image_url("https://image.tmdb.org/t/p/w1280/backdrop.jpg"))
        self.assertTrue(_is_allowed_image_url("https://upload.wikimedia.org/wikipedia/commons/a/ab/actor.jpg"))
        self.assertFalse(_is_allowed_image_url("https://image.tmdb.org.evil.example/backdrop.jpg"))
        self.assertFalse(_is_allowed_image_url("https://st.kp.yandex.net.evil.example/actor.jpg"))
        self.assertFalse(_is_allowed_image_url("file:///etc/passwd"))

    async def test_reads_every_chunk_of_an_image(self):
        stream = FakeImageStream([b"jpeg-start", b"-middle", b"-end"])
        self.assertEqual(await _read_image_limited(stream), b"jpeg-start-middle-end")

    async def test_rejects_an_image_that_exceeds_the_limit(self):
        stream = FakeImageStream([b"a" * _MAX_IMAGE_BYTES, b"b"])
        with self.assertRaises(HTTPException) as caught:
            await _read_image_limited(stream)
        self.assertEqual(caught.exception.status_code, 413)

    def test_rate_limit_rejects_only_after_configured_budget(self):
        for _ in range(ratelimit.IMAGE_PROXY_MAX):
            self.assertTrue(ratelimit.allow_image_proxy("audit-client"))
        self.assertFalse(ratelimit.allow_image_proxy("audit-client"))

    def test_cache_trim_evicts_oldest_files_to_honor_byte_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oldest = root / "oldest"
            newest = root / "newest"
            oldest.write_bytes(b"a" * 6)
            newest.write_bytes(b"b" * 6)
            os.utime(oldest, (time.time() - 60, time.time() - 60))

            old_dir = main._IMG_CACHE_DIR
            old_bytes = main._IMG_CACHE_MAX_BYTES
            old_files = main._IMG_CACHE_MAX_FILES
            try:
                main._IMG_CACHE_DIR = tmp
                main._IMG_CACHE_MAX_BYTES = 8
                main._IMG_CACHE_MAX_FILES = 10
                main._trim_image_cache_sync()
            finally:
                main._IMG_CACHE_DIR = old_dir
                main._IMG_CACHE_MAX_BYTES = old_bytes
                main._IMG_CACHE_MAX_FILES = old_files

            self.assertFalse(oldest.exists())
            self.assertTrue(newest.exists())
