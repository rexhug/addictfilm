import unittest

from main import FRONTEND_DIR, VersionedStaticFiles


class StaticAssetCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_versioned_assets_are_long_cached(self):
        static = VersionedStaticFiles(directory=FRONTEND_DIR)
        response = await static.get_response("app.js", {"method": "GET", "headers": []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
