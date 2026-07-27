"""Contract tests for the production asyncpg adapter.

They skip locally unless TEST_DATABASE_URL is supplied. CI provides a disposable
PostgreSQL service, so SQLite-only tests cannot accidentally mask SQL-dialect or
transaction regressions in the production path.
"""
import asyncio
import os
import unittest

import database as db
import db_runtime


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(TEST_DATABASE_URL, "requires TEST_DATABASE_URL")
class PostgresContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        await db_runtime.close()
        db.DATABASE_URL = TEST_DATABASE_URL
        db._PG = True
        await db.init_db()
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "TRUNCATE TABLE notification_deliveries, notifications, pair_event_recipients, pair_events, "
                "collection_films, collections, partners, partner_invites, "
                "movie_enrichment_jobs, movie_recommendation_profile_overrides, "
                "movie_recommendation_profiles, "
                "user_films, films, users, search_cache, search_budget RESTART IDENTITY CASCADE")
            await conn.commit()

    async def asyncTearDown(self):
        await db_runtime.close()
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg

    async def _add_users(self, *ids: int) -> None:
        for user_id in ids:
            await db.upsert_user({"id": user_id, "first_name": str(user_id), "username": None})

    async def test_invite_transaction_and_budget_use_postgres_placeholders(self):
        await self._add_users(1, 2)
        token = await db.create_invite(1)
        self.assertIsNotNone(token)

        result = await db.accept_invite(token, 2)

        self.assertTrue(result["ok"])
        self.assertEqual(result["partner_id"], 1)
        self.assertEqual(result["event"]["event_type"], "pair.invite.accepted")
        self.assertEqual(await db.get_partner(1), 2)
        self.assertEqual(await db.get_partner(2), 1)
        self.assertIsNone(await db.create_invite(1))
        self.assertTrue(await db.try_spend_search_budget("2099-01-01", 1))
        self.assertFalse(await db.try_spend_search_budget("2099-01-01", 1))

    async def test_catalog_first_lookup_and_negative_artwork_cache_work_on_postgres(self):
        film_id = await db.get_or_create_film(
            "tt0133093", "Матрица", title_original="The Matrix",
            actors="Киану Ривз", kp_id="301",
        )

        self.assertEqual(await db.get_film_id_by_source("k", "301"), film_id)
        items = await db.search_catalog("МАТРИЦА")
        self.assertEqual(items[0]["ref"], "tt0133093")

        self.assertTrue(await db.mark_film_artwork_checked("tt0133093", None))
        film = await db.get_film(film_id)
        self.assertIsNotNone(film["artwork_checked_at"])

        self.assertTrue(await db.mark_film_visuals_checked("tt0133093", None, None))
        film = await db.get_film(film_id)
        self.assertIsNotNone(film["poster_checked_at"])

    async def test_successful_notification_delivery_uses_a_postgres_boolean(self):
        """A sent delivery must work through asyncpg, not only SQLite.

        This catches accidental conversion of ``sent`` to ``0``/``1`` in the
        ``CASE WHEN`` expression. PostgreSQL rejects that integer parameter
        while SQLite silently accepts it.
        """
        await self._add_users(1)
        notification_id, created = await db.create_notification(
            event_type="pair.invite.accepted", recipient_id=1, actor_id=None,
            entity_id="event", payload={"title": "x"}, deep_link="stats",
            idempotency_key="postgres:delivery", event_id="postgres-event",
        )
        self.assertTrue(created)
        self.assertTrue(await db.create_notification_delivery(
            notification_id, channel="telegram", idempotency_key="postgres:telegram",
        ))

        await db.finish_notification_delivery(notification_id, channel="telegram", sent=True)

        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT status, attempts, sent_at FROM notification_deliveries "
                "WHERE notification_id = ? AND channel = ?",
                (notification_id, "telegram"),
            )).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["attempts"], 1)
        self.assertIsNotNone(row["sent_at"])


    async def test_two_workers_never_claim_the_same_enrichment_job(self):
        """SKIP LOCKED — единственное, что мешает двум воркерам взять одно задание.

        На SQLite это недоказуемо (там один писатель), поэтому настоящая
        блокировка проверяется только здесь, на живом PostgreSQL.
        """
        from enrichment import queue

        film_ids = [await db.get_or_create_film(f"tt7770{index}", f"Фильм {index}",
                                                media_type="movie") for index in range(6)]
        self.assertEqual(len(film_ids), 6)

        first, second = await asyncio.gather(
            queue.claim("worker-a", batch_size=3),
            queue.claim("worker-b", batch_size=3),
        )
        claimed = [job.id for job in first] + [job.id for job in second]
        # Ни одно задание не выдано дважды, и оба воркера получили работу.
        self.assertEqual(len(claimed), len(set(claimed)))
        self.assertEqual(len(claimed), 6)

    async def test_enqueue_is_idempotent_under_the_partial_unique_index(self):
        from enrichment import queue
        from enrichment.taxonomy import MOVIE_FEATURE_VERSION, MOVIE_TAXONOMY_VERSION

        film_id = await db.get_or_create_film("tt77799", "Фильм", media_type="movie")
        created = await queue.enqueue(film_id, feature_version=MOVIE_FEATURE_VERSION,
                                      taxonomy_version=MOVIE_TAXONOMY_VERSION)
        self.assertFalse(created)      # задание уже поставлено сохранением каталога
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT COUNT(*) AS n FROM movie_enrichment_jobs WHERE film_id = ?",
                (film_id,))).fetchone()
        self.assertEqual(row["n"], 1)

    async def test_profile_check_constraints_reject_out_of_range_values(self):
        """CHECK в схеме — последняя защита от кривого профиля."""
        film_id = await db.get_or_create_film("tt77788", "Фильм", media_type="movie")
        with self.assertRaises(Exception):
            async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
                await conn.execute(
                    "INSERT INTO movie_recommendation_profiles (film_id, status, content_type, "
                    "feature_version, taxonomy_version, extractor_version, source_hash, "
                    "energy, pace, tension, darkness, humor, emotionality, complexity, realism, "
                    "confidence, calculated_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (film_id, "ready", "movie", "v", "v", "v", "h",
                     0.5, 0.5, 0.5, 0.5, 9.9, 0.5, 0.5, 0.5, 0.5, "now", "now"))
                await conn.commit()
