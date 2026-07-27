"""Contract tests for the production asyncpg adapter.

They skip locally unless TEST_DATABASE_URL is supplied. CI provides a disposable
PostgreSQL service, so SQLite-only tests cannot accidentally mask SQL-dialect or
transaction regressions in the production path.
"""
import asyncio
import os
import unittest

import asyncpg
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
                "wishlist_random_picks, wishlist_random_state, recommendation_history, "
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
        with self.assertRaises(asyncpg.PostgresError):
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

    async def test_concurrent_enqueue_does_not_raise_a_unique_violation(self):
        """Раньше SELECT-затем-INSERT давал проигравшему исключение, а не False."""
        from enrichment import queue
        from enrichment.taxonomy import MOVIE_FEATURE_VERSION, MOVIE_TAXONOMY_VERSION

        film_id = await db.get_or_create_film("tt66601", "Гонка", media_type="movie")
        results = await asyncio.gather(*[
            queue.enqueue(film_id, feature_version=MOVIE_FEATURE_VERSION,
                          taxonomy_version=MOVIE_TAXONOMY_VERSION)
            for _ in range(5)
        ], return_exceptions=True)
        self.assertFalse([r for r in results if isinstance(r, BaseException)])
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT COUNT(*) AS n FROM movie_enrichment_jobs WHERE film_id = ?",
                (film_id,))).fetchone()
        self.assertEqual(row["n"], 1)      # задание уже создано сохранением каталога

    async def test_concurrent_profile_saves_are_atomic(self):
        from enrichment import extraction, repository
        from enrichment.models import NormalizedMovieSource
        from enrichment.taxonomy import ContentType

        film_id = await db.get_or_create_film("tt66602", "Профиль", media_type="movie")
        features = extraction.extract(NormalizedMovieSource(
            film_id=film_id, provider="kinopoisk", provider_id="1",
            content_type=ContentType.MOVIE, title="Профиль", genre_names=("драма",)))
        results = await asyncio.gather(*[
            repository.save_profile(film_id, features, status="ready", source_hash=f"h{i}",
                                    feature_version="v", taxonomy_version="v",
                                    extractor_version="v", semantic_model_version=None,
                                    validation_level="valid", warnings=())
            for i in range(5)
        ], return_exceptions=True)
        self.assertFalse([r for r in results if isinstance(r, BaseException)])
        profile = await repository.get_profile(film_id)
        self.assertEqual(profile["status"], "ready")
        self.assertTrue(set(profile["mood"]))
        self.assertIsNotNone(profile["source_hash"])

    async def test_reconciliation_is_idempotent_across_two_passes(self):
        """Главная защита от вечной петли changed_source → задание → тот же хеш."""
        from enrichment import queue, reconciliation, service
        from enrichment.semantic import DisabledSemanticClassifier

        await db.get_or_create_film("tt66603", "Идемпотентность", genres="драма",
                                    plot="История про расследование", media_type="movie",
                                    poster_url="https://example.test/p.jpg")
        while True:
            jobs = await queue.claim("pg-test", batch_size=10)
            if not jobs:
                break
            for job in jobs:
                await service.process_job(job, classifier=DisabledSemanticClassifier(),
                                          fetch_provider=False)
                await queue.complete(job.id)

        first = await reconciliation.reconcile(batch_size=100)
        second = await reconciliation.reconcile(batch_size=100)
        self.assertEqual(first.enqueued, 0)
        self.assertEqual(second.enqueued, 0)
        self.assertEqual(second.changed_source, 0)

    async def test_concurrent_init_db_does_not_race(self):
        """Веб и воркер стартуют одновременно и оба зовут init_db."""
        results = await asyncio.gather(db.init_db(), db.init_db(), return_exceptions=True)
        self.assertFalse([r for r in results if isinstance(r, BaseException)])


    async def test_concurrent_wishlist_roulette_never_repeats_inside_a_cycle(self):
        """SQLite это недоказуемо: там писатель один. Настоящая гонка — здесь."""
        await self._add_users(1)
        film_ids = []
        for index in range(5):
            film_id = await db.get_or_create_film(f"tt5550{index}", f"Фильм {index}",
                                                  media_type="movie")
            await db.add_to_list(1, film_id, "want_to_watch", None)
            film_ids.append(film_id)

        results = await asyncio.gather(*[db.pick_random_wishlist_film(1) for _ in range(5)])
        picked = [movie["id"] for movie in results if movie]
        self.assertEqual(len(picked), 5)
        self.assertEqual(sorted(picked), sorted(film_ids))
        self.assertEqual(len(set(picked)), 5, "фильм выдан дважды в одном круге")

    async def test_concurrent_smart_random_does_not_duplicate_a_show(self):
        from unittest.mock import patch

        import recommendations as rec

        async def _keep(_user_id, _partner_id, _weights, candidates, _minimum):
            return candidates

        await self._add_users(1)
        for index in range(6):
            await db.get_or_create_film(f"tt5560{index}", f"Кино {index}", genres="драма",
                                        imdb_rating="7.8", imdb_votes="200000",
                                        plot="История", media_type="movie",
                                        poster_url="https://example.test/p.jpg")
        with patch("recommendations._warm_catalog_if_sparse", new=_keep):
            items = await asyncio.gather(*[rec.pick_and_record_smart_random(1, "ru")
                                           for _ in range(4)])
        picked = [item["id"] for item in items if item]
        self.assertEqual(len(set(picked)), len(picked), "один фильм показан дважды")

        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            row = await (await conn.execute(
                "SELECT COUNT(*) AS n FROM recommendation_history "
                "WHERE user_id = 1 AND mode = 'random' AND action = 'shown'")).fetchone()
        self.assertEqual(row["n"], len(picked), "число записей показа не совпало с выдачей")

    async def test_recommendation_pick_lock_is_released(self):
        async with db.recommendation_pick_lock(1):
            pass
        # Повторный захват после освобождения не должен блокироваться.
        await asyncio.wait_for(self._reacquire_pick_lock(), timeout=5)

    async def _reacquire_pick_lock(self):
        async with db.recommendation_pick_lock(1):
            return True
