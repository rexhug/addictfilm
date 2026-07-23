import tempfile
import unittest
from pathlib import Path

import database as db
import db_runtime


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

    async def test_create_invite_is_idempotent_and_keeps_one_pending_token(self):
        await self._add_users(1)

        first = await db.create_invite(1)
        second = await db.create_invite(1)

        self.assertEqual(first, second)
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM partner_invites WHERE from_user = ? AND status = 'pending'", (1,))
            self.assertEqual((await cur.fetchone())[0], 1)

    async def test_accepting_a_pair_revokes_both_users_pending_invites(self):
        await self._add_users(1, 2)
        inviter_token = await db.create_invite(1)
        accepting_user_token = await db.create_invite(2)

        result = await db.accept_invite(inviter_token, 2)

        self.assertTrue(result["ok"])
        self.assertIsNone(await db.get_pending_invite(1))
        self.assertIsNone(await db.get_pending_invite(2))
        self.assertNotEqual(inviter_token, accepting_user_token)

    async def test_paired_user_cannot_create_a_late_pending_invite(self):
        """Regression for the invite/accept interleaving that left a stale link."""
        await self._add_users(1, 2)
        token = await db.create_invite(1)
        self.assertTrue((await db.accept_invite(token, 2))["ok"])

        self.assertIsNone(await db.create_invite(1))
        self.assertIsNone(await db.get_pending_invite(1))

    async def test_unpair_does_not_delete_a_nonreciprocal_new_partner_link(self):
        """A delayed unpair must only remove the pair it actually observed."""
        await self._add_users(1, 2, 3)
        token = await db.create_invite(1)
        self.assertTrue((await db.accept_invite(token, 2))["ok"])

        # Simulate a concurrent replacement of user 2's link after user 1's
        # relationship was established. The DB method must not delete 2 -> 3.
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute("UPDATE partners SET partner_id = ? WHERE user_id = ?", (3, 2))
            await conn.commit()

        await db.unpair(1)

        self.assertIsNone(await db.get_partner(1))
        self.assertEqual(await db.get_partner(2), 3)
