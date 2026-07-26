import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import database as db
import db_runtime


class PairNotificationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "notifications.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()
        for user_id in (1, 2, 3):
            await db.upsert_user({"id": user_id, "first_name": f"User {user_id}", "username": None})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def test_inbox_is_idempotent_and_read_state_is_scoped_to_recipient(self):
        first_id, created = await db.create_notification(
            event_type="pair.invite.created", recipient_id=2, actor_id=1, entity_id="token",
            payload={"title": "Invite", "body": "Join"}, deep_link="inv_token", idempotency_key="invite:token:2:inapp")
        second_id, duplicate = await db.create_notification(
            event_type="pair.invite.created", recipient_id=2, actor_id=1, entity_id="token",
            payload={"title": "Invite", "body": "Join"}, deep_link="inv_token", idempotency_key="invite:token:2:inapp")
        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(first_id, second_id)
        inbox = await db.list_notifications(2)
        self.assertEqual(inbox["unread_count"], 1)
        self.assertEqual(inbox["items"][0]["actor"]["name"], "User 1")
        self.assertTrue(await db.mark_notification_read(2, first_id))
        self.assertEqual((await db.list_notifications(2))["unread_count"], 0)
        self.assertFalse(await db.mark_notification_read(3, first_id))

    async def test_settings_and_all_pair_event_types_are_durable(self):
        settings = await db.update_notification_settings(2, language="en", telegram_enabled=False)
        self.assertEqual(settings, {"language": "en", "telegram_enabled": False})
        for number, event_type in enumerate((
            "pair.invite.created", "pair.invite.accepted", "pair.invite.declined",
            "pair.invite.cancelled", "pair.invite.expired", "pair.ended",
        )):
            await db.create_notification(
                event_type=event_type, recipient_id=2, actor_id=1, entity_id=str(number),
                payload={"title": event_type}, deep_link="stats", idempotency_key=f"{event_type}:{number}:2")
        inbox = await db.list_notifications(2, limit=10)
        self.assertEqual({item["event_type"] for item in inbox["items"]}, {
            "pair.invite.created", "pair.invite.accepted", "pair.invite.declined",
            "pair.invite.cancelled", "pair.invite.expired", "pair.ended",
        })
        self.assertEqual(await db.mark_all_notifications_read(2), 6)

    async def test_opened_invite_has_known_recipient_and_can_expire(self):
        token = await db.create_invite(1)
        self.assertTrue(await db.register_invite_recipient(token, 2))
        self.assertFalse(await db.register_invite_recipient(token, 2))
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute("UPDATE partner_invites SET expires_at = ? WHERE token = ?", (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), token))
            await conn.commit()
        expired = await db.expire_pending_invites()
        self.assertEqual(expired, [{"token": token, "from_user": 1, "recipient_ids": [2]}])
        self.assertIsNone(await db.get_invite_sender(token))
