import unittest

from fastapi import HTTPException

from main import _MAX_IMAGE_BYTES, _read_image_limited


class FakeImageStream:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class ImageProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_every_chunk_of_an_image(self):
        stream = FakeImageStream([b"jpeg-start", b"-middle", b"-end"])
        self.assertEqual(await _read_image_limited(stream), b"jpeg-start-middle-end")

    async def test_rejects_an_image_that_exceeds_the_limit(self):
        stream = FakeImageStream([b"a" * _MAX_IMAGE_BYTES, b"b"])
        with self.assertRaises(HTTPException) as caught:
            await _read_image_limited(stream)
        self.assertEqual(caught.exception.status_code, 413)
