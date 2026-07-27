"""Непрерывный цикл каталога: фильм появился → профиль готов → фильм изменился.

Это главная проверка задачи: владелец не должен размечать фильмы руками, значит
весь путь обязан проходить сам, переживать перезапуск и не терять ручные правки.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db
import recommendations
from enrichment import queue, reconciliation, repository, service, worker
from enrichment.models import PROFILE_READY
from enrichment.semantic import DisabledSemanticClassifier
from enrichment.taxonomy import MOVIE_FEATURE_VERSION, MOVIE_TAXONOMY_VERSION, ContentType


async def _keep_local_catalog(_user_id, _partner_id, _weights, candidates, _minimum):
    return candidates


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "life.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _film(self, imdb_id="tt100", *, title="Чёрная комедия", genres="комедия, криминал",
                    plot="Чёрная комедия о неудачном ограблении и жадных наследниках.",
                    kind="movie", runtime="110 мин"):
        return await db.get_or_create_film(
            imdb_id=imdb_id, title=title, genres=genres, plot=plot, runtime=runtime,
            imdb_rating="7.4", imdb_votes="150000", poster_url="https://example.test/p.jpg",
            media_type=kind)

    async def _drain(self) -> list:
        """Прогнать очередь так же, как это делает воркер."""
        results = []
        while True:
            jobs = await queue.claim("test-worker", batch_size=10)
            if not jobs:
                return results
            for job in jobs:
                results.append(await service.process_job(
                    job, classifier=DisabledSemanticClassifier(), fetch_provider=False))
                await queue.complete(job.id)

    async def test_new_film_travels_the_whole_lifecycle_automatically(self):
        film_id = await self._film()
        # 1. Задание создано самим сохранением каталога.
        self.assertEqual((await queue.stats()).get("pending"), 1)
        # 2. Обработка даёт готовый профиль.
        await self._drain()
        profile = await repository.get_profile(film_id)
        self.assertIsNotNone(profile)
        self.assertEqual(profile["status"], PROFILE_READY)
        self.assertEqual(profile["content_type"], str(ContentType.MOVIE))
        self.assertIn("dark_comedy", profile["tones"])
        self.assertEqual(profile["feature_version"], MOVIE_FEATURE_VERSION)
        self.assertEqual(profile["taxonomy_version"], MOVIE_TAXONOMY_VERSION)

    async def test_unchanged_film_is_not_recalculated(self):
        film_id = await self._film()
        await self._drain()
        first = await repository.get_profile(film_id)
        await queue.enqueue(film_id, feature_version=MOVIE_FEATURE_VERSION,
                            taxonomy_version=MOVIE_TAXONOMY_VERSION)
        results = await self._drain()
        self.assertTrue(results[0]["skipped"])
        self.assertEqual((await repository.get_profile(film_id))["calculated_at"],
                         first["calculated_at"])

    async def test_changed_source_triggers_recalculation(self):
        film_id = await self._film()
        await self._drain()
        before = await repository.get_profile(film_id)
        # Каталог обновился: описание стало другим.
        await db.get_or_create_film(imdb_id="tt100", title="Чёрная комедия",
                                    genres="комедия, криминал, драма",
                                    plot="Совсем другое описание про уютную деревню и доброту.",
                                    poster_url="https://example.test/p.jpg")
        await self._drain()
        after = await repository.get_profile(film_id)
        self.assertNotEqual(before["source_hash"], after["source_hash"])

    async def test_manual_override_survives_recalculation(self):
        film_id = await self._film()
        await self._drain()
        await repository.set_override(film_id, {"mood": {"humor": 0.05}},
                                      reason="ручная проверка", created_by=1)
        await queue.enqueue(film_id, feature_version="movie-mood-vNEXT",
                            taxonomy_version=MOVIE_TAXONOMY_VERSION)
        await self._drain()
        self.assertAlmostEqual((await repository.get_profile(film_id))["mood"]["humor"], 0.05)

    async def test_reconciler_finds_a_film_added_without_the_hook(self):
        film_id = await self._film()
        await self._drain()
        # Имитируем запись, приехавшую мимо хука: профиль стираем.
        import db_runtime
        from config import DATABASE_URL
        async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
            await conn.execute("DELETE FROM movie_recommendation_profiles WHERE film_id=?",
                               (film_id,))
            await conn.commit()
        report = await reconciliation.reconcile(batch_size=10)
        self.assertEqual(report.missing_profile, 1)
        self.assertEqual(report.enqueued, 1)

    async def test_reconciler_dry_run_writes_nothing(self):
        await self._film()
        before = await queue.stats()
        report = await reconciliation.reconcile(batch_size=10, dry_run=True)
        self.assertEqual(await queue.stats(), before)
        self.assertEqual(report.enqueued, 0)

    async def test_reconciler_skips_a_ready_unchanged_profile(self):
        await self._film()
        await self._drain()
        report = await reconciliation.reconcile(batch_size=10)
        self.assertEqual(report.enqueued, 0)

    async def test_reconciler_reenqueues_a_stale_version(self):
        film_id = await self._film()
        await self._drain()
        await repository.mark_stale("movie-mood-vNEXT", MOVIE_TAXONOMY_VERSION)
        report = await reconciliation.reconcile(batch_size=10)
        self.assertGreaterEqual(report.stale_version, 1)
        self.assertEqual(report.reasons.get(film_id), "stale_version")

    async def test_reconciler_does_not_duplicate_an_active_job(self):
        await self._film()
        report = await reconciliation.reconcile(batch_size=10)
        self.assertEqual(report.enqueued, 0)
        self.assertEqual(report.skipped_active, 1)

    async def test_worker_survives_a_failing_film_and_keeps_going(self):
        good = await self._film("tt200", title="Хороший")
        await self._film("tt201", title="Плохой")
        with patch("enrichment.service.process_job",
                   side_effect=[RuntimeError("провайдер лёг"),
                                {"film_id": good, "status": "ready", "skipped": False,
                                 "duration_ms": 1}]):
            jobs = await queue.claim("w", batch_size=2)
            outcomes = [await worker.process_one(job, classifier=DisabledSemanticClassifier())
                        for job in jobs]
        self.assertIn("retry", outcomes)
        self.assertIn("ready", outcomes)

    async def test_hundred_films_produce_no_duplicate_jobs(self):
        for index in range(100):
            await self._film(f"tt9{index:03d}", title=f"Фильм {index}")
        stats = await queue.stats()
        self.assertEqual(stats.get("pending"), 100)
        await self._drain()
        self.assertEqual((await queue.stats()).get("completed"), 100)
        distribution = await repository.distribution()
        self.assertEqual(sum(distribution["by_status"].values()), 100)


class RecommendationIntegrationTests(LifecycleTests):
    async def test_ready_profile_is_used_by_the_mood_layer(self):
        film_id = await self._film()
        await self._drain()
        candidates = await recommendations._attach_profiles(
            [{"id": film_id, "title": "Чёрная комедия", "genres": "комедия, криминал"}])
        features = recommendations.profile_mood_features(candidates[0])
        self.assertIsNotNone(features)
        self.assertIn("dark_comedy", features.markers)

    async def test_film_without_a_profile_uses_the_previous_path(self):
        """Отсутствие обогащения не должно менять поведение продукта."""
        self.assertIsNone(recommendations.profile_mood_features({"id": 1, "genres": "драма"}))

    async def test_low_confidence_profile_is_not_trusted_for_strict_mode(self):
        film_id = await self._film("tt300", title="Без метаданных", genres="", plot="")
        await self._drain()
        profile = await repository.get_profile(film_id)
        self.assertEqual(profile["status"], "low_confidence")
        candidates = await recommendations._attach_profiles([{"id": film_id, "genres": ""}])
        self.assertIsNone(recommendations.profile_mood_features(candidates[0]))

    async def test_tv_profile_never_reaches_movie_recommendations(self):
        await self._film("tt400", title="Сериал", kind="series", runtime="45 мин")
        movie_id = await self._film("tt401", title="Фильм")
        await self._drain()
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            ranked = await recommendations.ranked_candidates(
                1, partner_id=None, weights={}, filters={}, context={}, minimum=1)
        self.assertEqual([movie["id"] for movie in ranked], [movie_id])

    async def test_enrichment_failure_never_removes_a_film_from_the_pool(self):
        movie_id = await self._film("tt500", title="Фильм")
        # Профиля нет вовсе — фильм обязан остаться доступным в подборе.
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            ranked = await recommendations.ranked_candidates(
                1, partner_id=None, weights={}, filters={}, context={}, minimum=1)
        self.assertIn(movie_id, [movie["id"] for movie in ranked])

    async def test_profile_repository_failure_does_not_break_recommendations(self):
        movie_id = await self._film("tt600", title="Фильм")
        with patch("enrichment.repository.get_profiles", side_effect=RuntimeError("БД недоступна")):
            candidates = await recommendations._attach_profiles([{"id": movie_id}])
        self.assertEqual(len(candidates), 1)


if __name__ == "__main__":
    unittest.main()
