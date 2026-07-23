import unittest

from fastapi import HTTPException

from main import _MAX_IMAGE_BYTES, _is_allowed_image_url, _read_image_limited


class FakeImageStream:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class ImageProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_accepts_only_configured_image_hosts(self):
        self.assertTrue(_is_allowed_image_url("https://st.kp.yandex.net/images/actor.jpg"))
        self.assertTrue(_is_allowed_image_url("https://avatars.mds.yandex.net/get-kinopoisk-image/a/360"))
        self.assertTrue(_is_allowed_image_url("https://image.tmdb.org/t/p/w1280/backdrop.jpg"))
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
