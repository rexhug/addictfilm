import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import database as db
import db_runtime
import pair_notifications


class PairNotificationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        self.old_bot_token = pair_notifications.BOT_TOKEN
        db.DB_PATH = str(Path(self.temp_dir.name) / "notifications.db")
        db.DATABASE_URL = ""
        db._PG = False
        # The service is tested as an outbox here: no test can accidentally hit
        # the real Bot API, while channel selection remains fully covered.
        pair_notifications.BOT_TOKEN = ""
        await db.init_db()
        for user_id, name, username in ((1, "Alex", None), (2, "Alex", None), (3, "Casey", "casey"), (4, "Robin", None)):
            await db.upsert_user({"id": user_id, "first_name": name, "username": username})

    async def asyncTearDown(self):
        pair_notifications.BOT_TOKEN = self.old_bot_token
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def _dispatch(self, event):
        await pair_notifications.dispatch_pair_event(event=event)

    async def _items(self, user_id):
        return (await db.list_notifications(user_id, limit=20))["items"]

    async def test_inbox_is_idempotent_and_read_state_is_scoped_to_recipient(self):
        first_id, created = await db.create_notification(
            event_type="pair.invite.accepted", recipient_id=2, actor_id=1, entity_id="event",
            payload={"title": "Accepted", "body": "Join"}, deep_link="stats", idempotency_key="event:2:inapp", event_id="event")
        second_id, duplicate = await db.create_notification(
            event_type="pair.invite.accepted", recipient_id=2, actor_id=1, entity_id="event",
            payload={"title": "Accepted", "body": "Join"}, deep_link="stats", idempotency_key="event:2:inapp", event_id="event")
        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(first_id, second_id)
        self.assertEqual((await db.list_notifications(2))["unread_count"], 1)
        self.assertTrue(await db.mark_notification_read(2, first_id))
        self.assertEqual((await db.list_notifications(2))["unread_count"], 0)
        self.assertFalse(await db.mark_notification_read(3, first_id))

    async def test_delivery_completion_keeps_the_case_condition_boolean_typed(self):
        """PostgreSQL rejects SQLite's permissive 0/1 boolean shorthand."""
        captured: list[tuple[str, tuple]] = []

        class _Connection:
            async def execute(self, sql, parameters=()):
                captured.append((sql, tuple(parameters)))

            async def commit(self):
                pass

        @asynccontextmanager
        async def fake_connect(*_args, **_kwargs):
            yield _Connection()

        with patch.object(db_runtime, "connect", fake_connect):
            await db.finish_notification_delivery(42, channel="telegram", sent=True)

        self.assertIs(captured[0][1][3], True)

    async def test_opening_direct_invite_never_creates_duplicate_notification(self):
        token = await db.create_invite(1)
        self.assertTrue(await db.register_invite_recipient(token, 2))
        # The direct invite card is the invitation.  The old implementation
        # emitted pair.invite.created here, duplicating it in both channels.
        self.assertEqual(await self._items(1), [])
        self.assertEqual(await self._items(2), [])

    async def test_accept_uses_recipient_roles_and_never_reverses_people(self):
        token = await db.create_invite(1)
        await db.register_invite_recipient(token, 2)
        accepted = await db.accept_invite(token, 2)
        self.assertTrue(accepted["ok"])
        event = accepted["event"]
        self.assertEqual(event["inviter_user_id"], 1)
        self.assertEqual(event["invitee_user_id"], 2)
        self.assertEqual(event["actor_user_id"], 2)
        self.assertEqual(await db.get_pair_event_recipients(event["id"]), [
            {"recipient_user_id": 1, "partner_user_id": 2},
            {"recipient_user_id": 2, "partner_user_id": 1},
        ])

        await self._dispatch(event)
        await self._dispatch(event)  # retry is idempotent by event + recipient + channel
        inviter_item, = await self._items(1)
        invitee_item, = await self._items(2)
        self.assertEqual(inviter_item["payload"]["title"], "Теперь вы в паре")
        self.assertEqual(inviter_item["payload"]["body"], "Вы теперь в паре с Alex. Общая статистика уже доступна.")
        self.assertEqual(invitee_item["payload"]["title"], "Теперь вы в паре")
        self.assertEqual(invitee_item["payload"]["body"], "Вы теперь в паре с Alex. Общая статистика уже доступна.")
        self.assertEqual(invitee_item["payload"]["roles"]["recipient_user_id"], 2)
        self.assertEqual(invitee_item["payload"]["roles"]["partner_user_id"], 1)
        # The accepting person gets in-app confirmation only, never a bot echo.
        self.assertEqual(pair_notifications.delivery_channels(self._context(event, 2)), ("inapp",))
        self.assertEqual(pair_notifications.delivery_channels(self._context(event, 1)), ("inapp", "telegram"))

    async def test_each_recipient_uses_its_own_language_and_missing_users_do_not_break_delivery(self):
        await db.update_notification_settings(2, language="en")
        token = await db.create_invite(1)
        accepted = await db.accept_invite(token, 2)
        await self._dispatch(accepted["event"])
        self.assertEqual((await self._items(1))[0]["payload"]["title"], "Теперь вы в паре")
        self.assertEqual((await self._items(2))[0]["payload"]["title"], "You're now paired")

        # A deleted/anonymous recipient must never crash a retry worker.
        token = await db.create_invite(3)
        await db.register_invite_recipient(token, 4)
        cancelled = await db.unpair(3)
        original_get_user = db.get_user

        async def deleted_user(user_id):
            return None if user_id == 4 else await original_get_user(user_id)

        with patch.object(db, "get_user", side_effect=deleted_user):
            await self._dispatch(cancelled["event"])
        self.assertEqual(await self._items(4), [])

    async def test_decline_notifies_only_the_original_inviter(self):
        token = await db.create_invite(3)
        await db.register_invite_recipient(token, 4)
        declined = await db.decline_invite(token, 4)
        self.assertTrue(declined["ok"])
        await self._dispatch(declined["event"])
        inviter_item, = await self._items(3)
        self.assertEqual(inviter_item["payload"]["title"], "Приглашение отклонено")
        self.assertIn("Robin", inviter_item["payload"]["body"])
        self.assertEqual(await self._items(4), [])

    async def test_cancel_notifies_only_known_invitees(self):
        token = await db.create_invite(3)
        await db.register_invite_recipient(token, 4)
        cancelled = await db.unpair(3)
        self.assertEqual(cancelled["kind"], "cancelled")
        self.assertEqual(cancelled["token"], token)
        await self._dispatch(cancelled["event"])
        invitee_item, = await self._items(4)
        self.assertEqual(invitee_item["payload"]["title"], "Приглашение отменено")
        self.assertIn("Casey", invitee_item["payload"]["body"])
        self.assertEqual(await self._items(3), [])

    async def test_pair_end_notifies_only_former_partner(self):
        token = await db.create_invite(1)
        accepted = await db.accept_invite(token, 2)
        self.assertTrue(accepted["ok"])
        ended = await db.unpair(1)
        self.assertEqual(ended["kind"], "ended")
        await self._dispatch(ended["event"])
        self.assertEqual(await self._items(1), [])
        partner_item, = await self._items(2)
        self.assertEqual(partner_item["payload"]["title"], "Пара завершена")
        self.assertEqual(partner_item["payload"]["body"], "Пара с Alex завершена. Совместная статистика сохранена.")
        self.assertEqual(pair_notifications.delivery_channels(self._context(ended["event"], 1)), ())

    async def test_inbox_categories_filter_rows_and_report_global_unread_counts(self):
        events = (
            ("pair.invite.accepted", "pair"),
            ("pair.film.rated", "films"),
            ("account.security", "system"),
        )
        ids = {}
        for event_type, category in events:
            notification_id, created = await db.create_notification(
                event_type=event_type, recipient_id=1, actor_id=2, entity_id=event_type,
                payload={"title": category, "body": category}, deep_link="stats",
                idempotency_key=f"category:{category}", event_id=f"event:{category}",
            )
            self.assertTrue(created)
            ids[category] = notification_id

        all_result = await db.list_notifications(1, category="all")
        self.assertEqual(all_result["unread_count"], 3)
        self.assertEqual(all_result["unread_by_category"], {
            "all": 3, "pair": 1, "films": 1, "system": 1,
        })
        for _event_type, category in events:
            result = await db.list_notifications(1, category=category)
            self.assertEqual([item["id"] for item in result["items"]], [ids[category]])
            self.assertEqual(result["unread_by_category"], all_result["unread_by_category"])

        self.assertTrue(await db.mark_notification_read(1, ids["films"]))
        filtered = await db.list_notifications(1, category="pair")
        self.assertEqual(filtered["unread_by_category"], {
            "all": 2, "pair": 1, "films": 0, "system": 1,
        })
        with self.assertRaises(ValueError):
            await db.list_notifications(1, category="unknown")

    async def test_expiration_is_in_app_only_and_keeps_canonical_event(self):
        token = await db.create_invite(1)
        await db.register_invite_recipient(token, 2)
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute("UPDATE partner_invites SET expires_at = ? WHERE token = ?", (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), token))
            await conn.commit()
        expired, = await db.expire_pending_invites()
        self.assertEqual(expired["event"]["event_type"], "pair.invite.expired")
        await self._dispatch(expired["event"])
        self.assertEqual((await self._items(1))[0]["payload"]["title"], "Приглашение истекло")
        self.assertEqual((await self._items(2))[0]["payload"]["title"], "Приглашение истекло")
        self.assertEqual(pair_notifications.delivery_channels(self._context(expired["event"], 1)), ("inapp",))
        self.assertEqual(pair_notifications.delivery_channels(self._context(expired["event"], 2)), ("inapp",))

    async def test_blocked_bot_is_recorded_without_affecting_the_pair_event(self):
        notification_id, _ = await db.create_notification(
            event_type="pair.invite.declined", recipient_id=1, actor_id=2, entity_id="event",
            payload={"title": "x", "body": "y"}, deep_link="stats", idempotency_key="blocked:inapp", event_id="evt_blocked")
        self.assertTrue(await db.create_notification_delivery(
            notification_id, channel="telegram", idempotency_key="blocked:telegram"))

        async def blocked_bot(**_kwargs):
            raise RuntimeError("Telegram 403: bot was blocked by the user")

        with patch.object(pair_notifications, "_send_telegram", side_effect=blocked_bot):
            await pair_notifications._deliver(notification_id, 1, {"title": "x", "body": "y", "action_label": "Open", "deep_link": "stats"})
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT status, attempts, last_error FROM notification_deliveries WHERE notification_id = ? AND channel = 'telegram'",
                (notification_id,))).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertEqual(row[1], 1)
        self.assertIn("403", row[2])
        # A blocked bot is terminal: the hourly recovery worker must not keep
        # retrying a recipient who explicitly blocked the bot.
        self.assertEqual(await pair_notifications.retry_notification_deliveries(), 0)

    async def test_transient_telegram_failure_is_recovered_from_durable_delivery(self):
        notification_id, _ = await db.create_notification(
            event_type="pair.invite.declined", recipient_id=1, actor_id=2, entity_id="event",
            payload={"title": "x", "body": "y", "action_label": "Open"}, deep_link="stats",
            idempotency_key="transient:inapp", event_id="evt_transient")
        self.assertTrue(await db.create_notification_delivery(
            notification_id, channel="telegram", idempotency_key="transient:telegram"))

        async def offline(**_kwargs):
            # A Telegram 5xx confirms that the provider did not accept the
            # message, unlike an ambiguous local connection interruption.
            raise RuntimeError("Telegram 500: temporary provider failure")

        with patch.object(pair_notifications, "_send_telegram", side_effect=offline), \
                patch("pair_notifications.asyncio.sleep", return_value=None):
            await pair_notifications._deliver(notification_id, 1, {
                "title": "x", "body": "y", "action_label": "Open", "deep_link": "stats",
            })

        delivered = asyncio.Event()

        async def online(**_kwargs):
            delivered.set()

        with patch.object(pair_notifications, "_send_telegram", side_effect=online):
            self.assertEqual(await pair_notifications.retry_notification_deliveries(), 1)
            await asyncio.wait_for(delivered.wait(), timeout=0.5)
            await asyncio.gather(*list(pair_notifications._tasks))

        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT status, attempts FROM notification_deliveries WHERE notification_id = ? AND channel = 'telegram'",
                (notification_id,))).fetchone()
        self.assertEqual(row[0], "sent")
        self.assertEqual(row[1], 2)

    async def test_stale_inflight_delivery_is_archived_not_replayed_after_a_deploy(self):
        notification_id, _ = await db.create_notification(
            event_type="pair.invite.declined", recipient_id=1, actor_id=2, entity_id="event",
            payload={"title": "x", "body": "y", "action_label": "Open"}, deep_link="stats",
            idempotency_key="stale:inapp", event_id="evt_stale",
        )
        self.assertTrue(await db.create_notification_delivery(
            notification_id, channel="telegram", idempotency_key="stale:telegram",
        ))
        self.assertTrue(await db.claim_notification_delivery(notification_id, channel="telegram"))
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "UPDATE notification_deliveries SET updated_at=? WHERE notification_id=? AND channel=?",
                ("2000-01-01T00:00:00+00:00", notification_id, "telegram"),
            )
            await conn.commit()

        with patch.object(pair_notifications, "_send_telegram") as send:
            self.assertEqual(await pair_notifications.retry_notification_deliveries(), 0)
        send.assert_not_called()

        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT status, last_error FROM notification_deliveries WHERE notification_id=? AND channel=?",
                (notification_id, "telegram"),
            )).fetchone()
        self.assertEqual(row[0], "abandoned")
        self.assertIn("not retried", row[1])

    def _context(self, event, recipient_id):
        users = {
            1: {"id": 1, "first_name": "Alex", "username": None},
            2: {"id": 2, "first_name": "Alex", "username": None},
            3: {"id": 3, "first_name": "Casey", "username": "casey"},
            4: {"id": 4, "first_name": "Robin", "username": None},
        }
        partner_id = pair_notifications._partner_id(event, recipient_id)
        return pair_notifications.RecipientContext(
            event=event, recipient_user_id=recipient_id, partner_user_id=partner_id, recipient=users[recipient_id],
            inviter=users.get(event.get("inviter_user_id")), invitee=users.get(event.get("invitee_user_id")),
            actor=users.get(event.get("actor_user_id")), partner=users.get(partner_id), language="ru")
