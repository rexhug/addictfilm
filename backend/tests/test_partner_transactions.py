import tempfile
import unittest
from pathlib import Path

import database as db


class PartnerTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def _add_users(self, *ids):
        for user_id in ids:
            await db.upsert_user({"id": user_id, "first_name": str(user_id), "username": None})

    async def test_accept_invite_creates_a_symmetric_pair(self):
        await self._add_users(1, 2)
        self.assertTrue(await db.ping())
        token = await db.create_invite(1)

        result = await db.accept_invite(token, 2)

        self.assertEqual(result, {"ok": True, "partner_id": 1})
        self.assertEqual(await db.get_partner(1), 2)
        self.assertEqual(await db.get_partner(2), 1)
        self.assertIsNone(await db.get_pending_invite(1))

    async def test_rejected_acceptance_rolls_back_every_change(self):
        await self._add_users(1, 2, 3)
        accepted_token = await db.create_invite(2)
        self.assertTrue((await db.accept_invite(accepted_token, 3))["ok"])
        token = await db.create_invite(1)

        result = await db.accept_invite(token, 2)

        self.assertEqual(result, {"ok": False, "reason": "already_paired"})
        self.assertIsNone(await db.get_partner(1))
        self.assertEqual(await db.get_partner(2), 3)
        self.assertEqual(await db.get_pending_invite(1), token)
