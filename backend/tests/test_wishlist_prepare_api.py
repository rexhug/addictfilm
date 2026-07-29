"""Signed two-phase wishlist prefetch contract."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import database as db
import main
from fastapi import HTTPException


class WishlistPrepareApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "wishlist-api.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One"})
        await db.upsert_user({"id": 2, "first_name": "Two"})
        self.film_id = await db.get_or_create_film(
            "tt-wishlist-api", "Prepared film", media_type="movie",
            poster_url="https://example.test/poster.jpg")
        await db.add_to_list(1, self.film_id, "want_to_watch", None)
        self.token_patch = patch.object(main, "BOT_TOKEN", "stable-test-bot-token")
        self.token_patch.start()

    async def asyncTearDown(self):
        self.token_patch.stop()
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_db
        self.temp.cleanup()

    async def test_prepare_then_consume_records_exact_card_and_returns_next(self):
        prepared = await main.wishlist_random_prepare({"id": 1})
        self.assertEqual(prepared["item"]["id"], self.film_id)
        self.assertTrue(prepared["token"])
        self.assertEqual((await db.wishlist_roulette_state(1))["shown_in_cycle"], 0)

        consumed = await main.wishlist_random_consume(
            main.WishlistPreparedConsumeBody(token=prepared["token"]), {"id": 1})
        self.assertEqual(consumed["item"]["id"], self.film_id)
        self.assertEqual(consumed["cycle"]["shown_in_cycle"], 1)
        self.assertEqual(consumed["next"]["item"]["id"], self.film_id)

    async def test_token_is_bound_to_user_and_signature(self):
        prepared = await main.wishlist_random_prepare({"id": 1})
        with self.assertRaises(HTTPException) as other:
            await main.wishlist_random_consume(
                main.WishlistPreparedConsumeBody(token=prepared["token"]), {"id": 2})
        self.assertEqual(other.exception.status_code, 403)

        token = prepared["token"]
        tampered = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"
        with self.assertRaises(HTTPException) as invalid:
            await main.wishlist_random_consume(
                main.WishlistPreparedConsumeBody(token=tampered), {"id": 1})
        self.assertEqual(invalid.exception.status_code, 400)

    async def test_expired_token_is_rejected_without_recording_show(self):
        with patch.object(main, "_WISHLIST_PREPARE_TOKEN_TTL", timedelta(seconds=-1)):
            prepared = await main.wishlist_random_prepare({"id": 1})
        with self.assertRaises(HTTPException) as expired:
            await main.wishlist_random_consume(
                main.WishlistPreparedConsumeBody(token=prepared["token"]), {"id": 1})
        self.assertEqual(expired.exception.status_code, 410)
        self.assertEqual((await db.wishlist_roulette_state(1))["shown_in_cycle"], 0)

    async def test_replayed_token_is_a_conflict(self):
        prepared = await main.wishlist_random_prepare({"id": 1})
        body = main.WishlistPreparedConsumeBody(token=prepared["token"])
        await main.wishlist_random_consume(body, {"id": 1})
        with self.assertRaises(HTTPException) as replay:
            await main.wishlist_random_consume(body, {"id": 1})
        self.assertEqual(replay.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
