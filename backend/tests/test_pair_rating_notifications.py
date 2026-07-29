import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database as db
import main
import pair_activity_notifications as activity


class PairRatingNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "pair-rating.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "Alex", "username": "alex"})
        await db.upsert_user({"id": 2, "first_name": "Sam", "username": "sam"})
        self.film_id = await db.get_or_create_film("tt9900001", "Точный фильм")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def _pair(self) -> None:
        token = await db.create_invite(1)
        result = await db.accept_invite(token, 2)
        self.assertTrue(result["ok"])

    async def _rate_and_notify(self, actor_id=1, rating=8) -> bool:
        result = await db.set_rating(actor_id, self.film_id, rating)
        return await activity.notify_partner_film_rated(
            actor_id=actor_id,
            film_id=self.film_id,
            rating=rating,
            first_rating=result.first_rating,
        )

    async def _items(self, user_id: int) -> list[dict]:
        return (await db.list_notifications(user_id, limit=20))["items"]

    async def test_first_rating_notifies_only_the_active_partner_in_app(self):
        await self._pair()

        self.assertTrue(await self._rate_and_notify())

        self.assertEqual(await self._items(1), [])
        items = await self._items(2)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["event_type"], activity.PAIR_FILM_RATED)
        self.assertEqual(item["recipient_id"], 2)
        self.assertEqual(item["actor"]["id"], 1)
        self.assertEqual(item["entity_id"], str(self.film_id))
        self.assertEqual(item["deep_link"], f"movie:{self.film_id}")
        self.assertEqual(item["payload"]["action_label"], "Оценить")
        self.assertEqual(
            item["payload"]["body"],
            "Новая оценка фильма «Точный фильм» от Alex. А теперь твоя очередь!",
        )

        async with db.db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            deliveries = await (await conn.execute(
                "SELECT COUNT(*) FROM notification_deliveries",
            )).fetchone()
        self.assertEqual(deliveries[0], 0)

    async def test_no_pair_and_rating_update_create_no_notification(self):
        self.assertFalse(await self._rate_and_notify())
        self.assertEqual(await self._items(2), [])

        await self._pair()
        # The score existed before this pair session, so editing it is not a
        # first rating and must not materialize pair activity retroactively.
        self.assertFalse(await self._rate_and_notify(rating=9))
        self.assertEqual(await self._items(2), [])

    async def test_duplicate_concurrent_materialization_is_idempotent(self):
        await self._pair()
        result = await db.set_rating(1, self.film_id, 7)

        created = await asyncio.gather(*[
            activity.notify_partner_film_rated(
                actor_id=1,
                film_id=self.film_id,
                rating=7,
                first_rating=result.first_rating,
            )
            for _ in range(2)
        ])

        self.assertEqual(sum(created), 1)
        self.assertEqual(len(await self._items(2)), 1)

    async def test_clear_and_rerate_does_not_spam_the_same_pair_session(self):
        await self._pair()
        self.assertTrue(await self._rate_and_notify(rating=7))
        await db.clear_rating(1, self.film_id)

        self.assertFalse(await self._rate_and_notify(rating=9))

        self.assertEqual(len(await self._items(2)), 1)

    async def test_new_pair_session_may_create_a_new_event_for_same_people(self):
        await self._pair()
        self.assertTrue(await self._rate_and_notify(rating=7))
        self.assertEqual((await db.unpair(1))["kind"], "ended")
        await db.clear_rating(1, self.film_id)
        await self._pair()

        self.assertTrue(await self._rate_and_notify(rating=8))

        self.assertEqual(len(await self._items(2)), 2)

    async def test_atomic_insert_rejects_pair_that_ended_after_context_read(self):
        await self._pair()
        result = await db.set_rating(1, self.film_id, 8)
        context = await db.get_partner_rating_notification_context(1, self.film_id)
        self.assertIsNotNone(context)
        await db.unpair(1)
        text = activity.format_partner_rating_notification(context=context, rating=8)

        notification_id, created = await db.create_notification_for_current_partner(
            actor_id=1,
            recipient_id=2,
            pair_since=context["pair_since"],
            event_type=activity.PAIR_FILM_RATED,
            entity_id=str(self.film_id),
            payload=text,
            deep_link=f"movie:{self.film_id}",
            idempotency_key=activity.rating_notification_key(
                pair_since=context["pair_since"],
                actor_id=1,
                recipient_id=2,
                film_id=self.film_id,
            ),
        )

        self.assertTrue(result.first_rating)
        self.assertIsNone(notification_id)
        self.assertFalse(created)
        self.assertEqual(await self._items(2), [])

    async def test_localized_copy_reveals_both_scores_only_after_recipient_rating(self):
        await self._pair()
        await db.update_notification_settings(2, language="en")
        context = await db.get_partner_rating_notification_context(1, self.film_id)
        hidden = activity.format_partner_rating_notification(context=context, rating=8)
        self.assertEqual(hidden["title"], "New partner rating")
        self.assertEqual(hidden["action_label"], "Rate movie")
        self.assertEqual(
            hidden["body"],
            "Alex rated “Точный фильм”. Now it’s your turn.",
        )

        await db.set_rating(2, self.film_id, 6)
        context = await db.get_partner_rating_notification_context(1, self.film_id)
        comparison = activity.format_partner_rating_notification(context=context, rating=8)
        self.assertEqual(comparison["title"], "New partner rating")
        self.assertEqual(comparison["action_label"], "Compare")
        self.assertEqual(
            comparison["body"],
            "“Точный фильм”: Alex rated it 8/10. Your rating is 6/10.",
        )

    async def test_rating_endpoint_survives_notification_failure(self):
        result = db.RatingWriteResult(previous_rating=None, rating=8)
        with (
            patch.object(main.db, "get_film", AsyncMock(return_value={"id": self.film_id})),
            patch.object(main.db, "set_rating", AsyncMock(return_value=result)),
            patch.object(main.db, "sync_film_to_partner", AsyncMock()),
            patch.object(main, "_invalidate_stats_for", AsyncMock()),
            patch.object(
                main.pair_activity_notifications,
                "notify_partner_film_rated",
                AsyncMock(side_effect=RuntimeError("inbox unavailable")),
            ),
        ):
            response = await main.rate(
                self.film_id,
                main.RateBody(rating=8),
                user={"id": 1},
            )

        self.assertEqual(response, {"ok": True})
