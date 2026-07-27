"""Авторизация админки: deny-by-default и только по проверенному Telegram ID.

Проверяем сам гейт (require_editor) и выдачу возможностей (/api/me/capabilities),
а не UI: тумблер «Режим администратора» во фронтенде прав не даёт.
"""
import unittest
from unittest.mock import patch

import main
from fastapi import HTTPException


class RequireEditorTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, role):
        with patch.object(main, "_effective_role", return_value=role):
            return await main.require_editor(user={"id": 42})

    async def test_ordinary_user_is_denied(self):
        for role in (None, "", "user", "viewer"):
            with self.subTest(role=role), self.assertRaises(HTTPException) as ctx:
                await self._call(role)
            self.assertEqual(ctx.exception.status_code, 403)

    async def test_editor_and_admin_are_allowed(self):
        for role in ("editor", "admin"):
            with self.subTest(role=role):
                self.assertEqual((await self._call(role))["id"], 42)

    async def test_denial_does_not_leak_who_is_admin(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._call(None)
        # Общий отказ без имён, ID и списка администраторов.
        self.assertNotIn("admin", str(ctx.exception.detail).lower())

    async def test_client_supplied_role_is_ignored(self):
        """Подделанные клиентом поля не участвуют в решении: роль берётся
        сервером по проверенному ID, а не из тела/заголовков запроса."""
        forged = {"id": 42, "role": "admin", "is_admin": True}
        with patch.object(main, "_effective_role", return_value=None), \
                self.assertRaises(HTTPException) as ctx:
            await main.require_editor(user=forged)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_role_is_resolved_by_verified_id_not_username(self):
        seen = {}

        async def fake_role(user_id):
            seen["arg"] = user_id
            return "editor"

        with patch.object(main, "_effective_role", side_effect=fake_role):
            await main.require_editor(user={"id": 777, "username": "someone_else"})
        self.assertEqual(seen["arg"], 777)


class CapabilitiesTests(unittest.IsolatedAsyncioTestCase):
    async def _capabilities(self, role):
        with patch.object(main, "_effective_role", return_value=role):
            return await main.me_capabilities(user={"id": 42})

    async def test_ordinary_user_gets_empty_capabilities(self):
        payload = await self._capabilities(None)
        self.assertEqual(payload, {"is_admin": False, "admin_role": None, "capabilities": []})

    async def test_editor_gets_write_capabilities(self):
        payload = await self._capabilities("editor")
        self.assertTrue(payload["is_admin"])
        self.assertEqual(payload["admin_role"], "editor")
        self.assertIn("collections.write", payload["capabilities"])

    async def test_editor_cannot_read_audit_log(self):
        editor = await self._capabilities("editor")
        admin = await self._capabilities("admin")
        self.assertNotIn("audit.read", editor["capabilities"])
        self.assertIn("audit.read", admin["capabilities"])

    async def test_revoked_admin_immediately_loses_capabilities(self):
        self.assertTrue((await self._capabilities("admin"))["is_admin"])
        # Роль отозвали — следующий же ответ пустой, кэш на клиенте не спасает.
        self.assertFalse((await self._capabilities(None))["is_admin"])


class PublicSerializerTests(unittest.IsolatedAsyncioTestCase):
    def test_public_collection_hides_editorial_metadata(self):
        row = {"id": 1, "title": "T", "description": "D", "cover": "c", "film_count": 2,
               "status": "published", "created_by": 111, "updated_at": "x", "version": 7,
               "published_at": "y", "sort_order": 0}
        public = main._public_collection(row)
        for leaked in ("created_by", "updated_at", "version", "status", "published_at"):
            self.assertNotIn(leaked, public)
        self.assertEqual(public["title"], "T")


if __name__ == "__main__":
    unittest.main()
