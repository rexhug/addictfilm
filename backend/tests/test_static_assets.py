import unittest
from pathlib import Path

from main import FRONTEND_DIR, VersionedStaticFiles


class StaticAssetCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_versioned_assets_are_long_cached(self):
        static = VersionedStaticFiles(directory=FRONTEND_DIR)
        response = await static.get_response("app.js", {"method": "GET", "headers": []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")

    async def test_detail_fallback_and_mobile_tab_hit_targets_are_present(self):
        root = Path(FRONTEND_DIR)
        css = (root / "style.css").read_text()
        app = (root / "app.js").read_text()
        index = (root / "index.html").read_text()

        self.assertIn(".d-backdrop.no-bd{aspect-ratio:16/7", css)
        self.assertIn("#tabbar .tab::before", css)
        self.assertIn('btn.addEventListener("pointerup"', app)
        self.assertIn("style.css?v=38", index)
        self.assertIn("app.js?v=43", index)
        self.assertIn("renderDetailPreview", app)
        self.assertIn("AbortController", app)
        self.assertIn("isKinopoiskPortraitPlaceholder", app)
        self.assertIn("data-person-photo", app)
        self.assertIn("resetDetailViewport", app)
        self.assertIn("overflow-anchor:none", css)
        self.assertIn("const _readCache", app)
        self.assertIn("prefers-reduced-motion:reduce){\n  #tabbar", css)
