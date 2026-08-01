"""Кэш фронтенда: деплой обязан доезжать до Telegram WebView.

Прошлый инцидент выглядел так: app.js отдавался как immutable на год, а ?v= в
index.html при очередном изменении файла не подняли. Формально всё было верно,
фактически пользователь оставался на старом интерфейсе и починить это мог
только сбросом кэша.

Здесь проверяется ровно та связка, которая тогда разошлась: номер версии в
разметке и заголовок, с которым сервер отдаёт сам файл. Раздача статики
вызывается напрямую — ни базы, ни HTTP-клиента для этого не нужно.
"""
import unittest
from pathlib import Path

from main import FRONTEND_DIR, VersionedStaticFiles

REVALIDATE = "no-cache, max-age=0, must-revalidate"
EXPECTED_APP_VERSION = "app.js?v=101"


def scope(path: str) -> dict:
    """Минимальный ASGI-запрос: ?v= сюда не попадает — это метка для кэша,
    маршрутизация идёт по самому пути."""
    return {"type": "http", "method": "GET", "headers": [], "path": "/" + path}


class FrontendCachePolicyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.static = VersionedStaticFiles(directory=FRONTEND_DIR)
        cls.index = Path(FRONTEND_DIR, "index.html").read_text(encoding="utf-8")

    async def _serve(self, path: str):
        return await self.static.get_response(path, scope(path))

    def test_the_markup_requests_the_current_bundle_version(self):
        self.assertIn(EXPECTED_APP_VERSION, self.index)
        # Именно этот номер завис в Telegram, пока файл под ним менялся.
        self.assertNotIn("app.js?v=100", self.index)

    async def test_the_bundle_is_revalidated_instead_of_being_cached_for_a_year(self):
        response = await self._serve("app.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], REVALIDATE)
        # Год immutable — ровно то, из-за чего старый интерфейс пережил деплой.
        self.assertNotIn("immutable", response.headers["Cache-Control"])

    async def test_styles_follow_the_same_policy_so_js_and_css_cannot_diverge(self):
        response = await self._serve("style.css")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], REVALIDATE)

    async def test_every_version_in_the_markup_resolves_to_a_served_file(self):
        """Разметка не должна просить файл, которого сервер не отдаёт: именно
        рассинхрон разметки и раздачи и был причиной инцидента."""
        import re

        for asset in re.findall(r'(?:src|href)="([^"?]+)(?:\?v=\d+)?"', self.index):
            if asset.startswith(("http://", "https://", "//", "#")):
                continue
            with self.subTest(asset=asset):
                response = await self._serve(asset)
                self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
