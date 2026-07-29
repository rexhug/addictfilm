"""Public movie reviews: privacy migration, pagination and moderation."""
import tempfile
import unittest
from pathlib import Path

import database as db
import db_runtime
import ratelimit


class PublicReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "reviews.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        for user_id, name in ((1, "Я"), (2, "Партнёр"), (3, "Третий"), (4, "Четвёртый")):
            await db.upsert_user({
                "id": user_id, "first_name": name, "username": f"u{user_id}",
                "photo_url": f"https://example.test/u{user_id}.jpg",
            })
        self.film_id = await db.get_or_create_film("tt-review-1", "Фильм")

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _pair(self, first=1, second=2):
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "INSERT INTO partners(user_id, partner_id, since) VALUES (?,?,?)",
                (first, second, "2026-01-01"),
            )
            await conn.execute(
                "INSERT INTO partners(user_id, partner_id, since) VALUES (?,?,?)",
                (second, first, "2026-01-01"),
            )
            await conn.commit()

    async def test_old_client_note_stays_private_and_is_not_listed(self):
        await db.set_rating(1, self.film_id, 8)
        await db.set_comment(1, self.film_id, "Старая заметка")

        context = await db.get_movie_rating_context(1, self.film_id)
        result = await db.list_movie_reviews(1, self.film_id)

        self.assertEqual(context["mine"]["comment_status"], "private_legacy")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)

    async def test_migration_marks_existing_comments_private_instead_of_publishing(self):
        await db.set_rating(1, self.film_id, 8)
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                """UPDATE user_films
                   SET comment=?, comment_status=NULL, commented_at=NULL
                   WHERE user_id=? AND film_id=?""",
                ("До публичных отзывов", 1, self.film_id),
            )
            await conn.commit()

        await db._apply_public_reviews_migration()

        context = await db.get_movie_rating_context(1, self.film_id)
        self.assertEqual(context["mine"]["comment_status"], "private_legacy")
        self.assertEqual((await db.list_movie_reviews(1, self.film_id))["total"], 0)

    async def test_legacy_note_requires_rating_and_explicit_publish(self):
        await db.set_comment(1, self.film_id, "Личная")
        self.assertIsNone(await db.publish_legacy_review(1, self.film_id))
        await db.set_rating(1, self.film_id, 9)

        item = await db.publish_legacy_review(1, self.film_id)

        self.assertEqual(item["text"], "Личная")
        self.assertEqual(item["rating"], 9)
        self.assertEqual((await db.list_movie_reviews(1, self.film_id))["total"], 1)

    async def test_edit_updates_one_stable_review_instead_of_duplicating(self):
        first = await db.set_public_review(1, self.film_id, 7, "Первая версия")
        second = await db.set_public_review(1, self.film_id, 10, "Новая версия")
        result = await db.list_movie_reviews(1, self.film_id)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["text"], "Новая версия")
        self.assertEqual(result["items"][0]["rating"], 10)

    async def test_partner_is_pinned_and_not_duplicated_in_items(self):
        await self._pair()
        await db.set_public_review(1, self.film_id, 8, "Мой")
        partner = await db.set_public_review(2, self.film_id, 9, "Партнёра")
        await db.set_public_review(3, self.film_id, 10, "Другой")

        result = await db.list_movie_reviews(1, self.film_id)

        self.assertEqual(result["partner_review"]["id"], partner["id"])
        self.assertTrue(result["partner_review"]["is_partner"])
        self.assertNotIn(partner["id"], [item["id"] for item in result["items"]])
        self.assertEqual(result["total"], 3)

    async def test_sorting_and_keyset_pagination_are_stable(self):
        for user_id, rating in ((1, 7), (2, 10), (3, 4), (4, 8)):
            await db.set_public_review(user_id, self.film_id, rating, f"Оценка {rating}")

        highest = await db.list_movie_reviews(1, self.film_id, limit=2, sort="highest")
        next_page = await db.list_movie_reviews(
            1, self.film_id, limit=2, sort="highest",
            before_id=highest["next_before_id"],
        )
        lowest = await db.list_movie_reviews(1, self.film_id, limit=4, sort="lowest")

        self.assertEqual([item["rating"] for item in highest["items"]], [10, 8])
        self.assertEqual([item["rating"] for item in next_page["items"]], [7, 4])
        self.assertIsNone(next_page["next_before_id"])
        self.assertEqual([item["rating"] for item in lowest["items"]], [4, 7, 8, 10])

    async def test_reports_are_unique_and_hidden_reviews_disappear(self):
        review = await db.set_public_review(2, self.film_id, 6, "Нарушение")

        self.assertTrue(await db.report_review(1, self.film_id, review["id"], "spam"))
        self.assertFalse(await db.report_review(1, self.film_id, review["id"], "again"))
        self.assertFalse(await db.report_review(2, self.film_id, review["id"], "own"))
        hidden = await db.hide_review(review["id"])

        self.assertEqual(hidden["user_id"], 2)
        self.assertEqual((await db.list_movie_reviews(1, self.film_id))["total"], 0)

    async def test_only_reciprocal_active_partner_rating_is_exposed(self):
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "INSERT INTO partners(user_id, partner_id, since) VALUES (?,?,?)",
                (1, 2, "2026-01-01"),
            )
            await conn.commit()
        await db.set_rating(2, self.film_id, 10)
        self.assertIsNone((await db.get_movie_rating_context(1, self.film_id))["partner"])

        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "INSERT INTO partners(user_id, partner_id, since) VALUES (?,?,?)",
                (2, 1, "2026-01-01"),
            )
            await conn.commit()
        partner = (await db.get_movie_rating_context(1, self.film_id))["partner"]
        self.assertEqual(partner["rating"], 10)

    async def test_review_mutations_have_an_independent_rate_limit(self):
        old_limit = ratelimit.REVIEW_WRITE_MAX
        old_window = ratelimit.REVIEW_WRITE_WINDOW
        try:
            ratelimit.REVIEW_WRITE_MAX = 2
            ratelimit.REVIEW_WRITE_WINDOW = 60
            ratelimit._reset_for_tests()

            self.assertTrue(ratelimit.allow_review_write(1001))
            self.assertTrue(ratelimit.allow_review_write(1001))
            self.assertFalse(ratelimit.allow_review_write(1001))
            self.assertTrue(ratelimit.allow_review_write(1002))
        finally:
            ratelimit.REVIEW_WRITE_MAX = old_limit
            ratelimit.REVIEW_WRITE_WINDOW = old_window
            ratelimit._reset_for_tests()


if __name__ == "__main__":
    unittest.main()
