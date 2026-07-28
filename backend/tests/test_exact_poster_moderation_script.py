from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import database as db

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))

import reject_exact_promotional_poster as operation


class ExactPosterModerationScriptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "operation.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        self.film_id = await db.get_or_create_film(
            imdb_id="tt4873118",
            title=operation.TARGET_TITLE,
            title_original=operation.TARGET_ORIGINAL_TITLE,
            poster_url="https://reviewed.example/promo.jpg",
            media_type="movie",
        )

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def test_inspection_locates_the_unique_row_without_a_hardcoded_id(self):
        found = await operation.locate_target()
        self.assertEqual(found["id"], self.film_id)
        self.assertEqual(found["poster_url"], "https://reviewed.example/promo.jpg")

    async def test_apply_is_bound_to_the_reviewed_current_url(self):
        with self.assertRaisesRegex(RuntimeError, "changed after review"):
            await operation.reject_exact_current_poster("https://reviewed.example/old.jpg")
        updated = await operation.reject_exact_current_poster(
            "https://reviewed.example/promo.jpg")
        self.assertEqual(updated["poster_display_state"], "rejected")
        self.assertEqual(
            updated["poster_reject_reason"], operation.REJECT_REASON)
        self.assertEqual(
            updated["poster_display_url"], "https://reviewed.example/promo.jpg")
