import unittest
from pathlib import Path

from main import FRONTEND_DIR, VersionedStaticFiles


class StaticAssetCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_versioned_assets_are_revalidated(self):
        """Раньше здесь ожидался immutable на год — и это ожидание оказалось
        дороже, чем выглядело: app.js изменили, ?v= поднять забыли, и Telegram
        законно держал годовалую копию. Подробности политики — в
        test_frontend_cache_policy.py."""
        static = VersionedStaticFiles(directory=FRONTEND_DIR)
        response = await static.get_response("app.js", {"method": "GET", "headers": []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-cache, max-age=0, must-revalidate")

    async def test_detail_fallback_and_mobile_tab_hit_targets_are_present(self):
        root = Path(FRONTEND_DIR)
        css = (root / "style.css").read_text()
        app = (root / "app.js").read_text()
        index = (root / "index.html").read_text()

        # Запасные экраны обязаны иметь ОСОЗНАННУЮ компактную геометрию. Раньше
        # это гарантировала одна строка `.d-backdrop.no-bd{aspect-ratio:16/7`;
        # теперь высота и наплыв тела задаются состоянием на корне карточки и
        # связаны через переменные. Сторожим сам контракт, а не пиксели: иначе
        # тест защищал бы конкретную старую реализацию, а не свойство продукта.
        self.assertIn(".detail-v2.detail-hero-poster{--detail-hero-height:", css)
        self.assertIn(".detail-v2.detail-hero-none{--detail-hero-height:", css)
        self.assertIn("--detail-hero-overlap", css)
        self.assertIn("margin-top:calc(-1 * var(--detail-hero-overlap))", css)
        # Прежняя жёсткая геометрия не должна вернуться незаметно.
        self.assertNotIn("aspect-ratio:16/7", css)
        self.assertIn("#tabbar .tab::before", css)
        self.assertIn('btn.addEventListener("pointerup"', app)
        # Asset versions deliberately change on every frontend release so a
        # Telegram WebView cannot keep stale CSS or JS.  Require a versioned
        # URL rather than coupling this regression test to one old release.
        self.assertRegex(index, r'style\.css\?v=\d+')
        self.assertRegex(index, r'app\.js\?v=\d+')
        self.assertIn("tg.disableVerticalSwipes?.()", app)
        self.assertIn("renderDetailPreview", app)
        self.assertIn("AbortController", app)
        self.assertIn("isKinopoiskPortraitPlaceholder", app)
        self.assertIn("data-person-photo", app)
        self.assertIn("resetDetailViewport", app)
        self.assertIn("overflow-anchor:none", css)
        self.assertIn("const _readCache", app)
        self.assertIn("prefers-reduced-motion:reduce){\n  #tabbar", css)
