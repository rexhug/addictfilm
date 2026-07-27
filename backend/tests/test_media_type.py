"""Подбор ФИЛЬМОВ не должен предлагать сериалы, эпизоды и короткий метр.

Дефект был живым: 22-минутный мультсериал «Жизнь и приключения робота-подростка»
попал в выдачу как фильм. Проверяем именно свойство — тип записи решает, а не
длительность (22 минуты бывают и у сериала, и у короткометражки).
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db
import media_type
import recommendations


async def _keep_local_catalog(_user_id, _partner_id, _weights, candidates, _minimum):
    return candidates


class ClassificationTests(unittest.TestCase):
    def test_provider_types_map_to_catalog_values(self):
        self.assertEqual(media_type.from_kinopoisk({"type": "animated-series"}), media_type.SERIES)
        self.assertEqual(media_type.from_kinopoisk({"type": "tv-series"}), media_type.SERIES)
        self.assertEqual(media_type.from_kinopoisk({"type": "cartoon"}), media_type.MOVIE)
        self.assertEqual(media_type.from_omdb({"Type": "series"}), media_type.SERIES)
        self.assertEqual(media_type.from_omdb({"Type": "episode"}), media_type.EPISODE)
        self.assertEqual(media_type.from_omdb({"Type": "movie"}), media_type.MOVIE)

    def test_ambiguous_anime_is_resolved_by_the_series_flag(self):
        """«anime» у kinopoisk — и полный метр, и сериал, но есть прямой признак."""
        self.assertEqual(media_type.from_kinopoisk({"type": "anime", "isSeries": True}), media_type.SERIES)
        self.assertEqual(media_type.from_kinopoisk({"type": "anime", "isSeries": False}), media_type.MOVIE)

    def test_missing_metadata_stays_unknown_instead_of_guessing(self):
        self.assertEqual(media_type.from_kinopoisk({"type": "anime"}), media_type.UNKNOWN)
        self.assertEqual(media_type.from_kinopoisk({}), media_type.UNKNOWN)

    def test_short_is_recognised_by_genre_not_by_runtime(self):
        short = {"media_type": "movie", "genres": "мультфильм, короткометражка", "runtime": "6 мин"}
        long_movie = {"media_type": "movie", "genres": "драма", "runtime": "22 мин"}
        self.assertEqual(media_type.resolve(short), media_type.SHORT)
        self.assertEqual(media_type.resolve(long_movie), media_type.MOVIE)
        self.assertFalse(media_type.is_movie_flow_eligible(short))
        self.assertTrue(media_type.is_movie_flow_eligible(long_movie))

    def test_twenty_two_minute_series_is_not_a_movie(self):
        cartoon_series = {"media_type": "series", "genres": "мультфильм, комедия", "runtime": "22 мин"}
        self.assertFalse(media_type.is_movie_flow_eligible(cartoon_series))

    def test_unknown_type_stays_eligible(self):
        """Legacy-записи без типа не должны обнулять подбор до конца бэкфила."""
        self.assertTrue(media_type.is_movie_flow_eligible({"genres": "драма", "runtime": "110 мин"}))


class CandidatePoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "t.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def _film(self, imdb_id, title, *, kind, genres="драма", runtime="110 мин", rating="8.5"):
        return await db.get_or_create_film(
            imdb_id=imdb_id, title=title, genres=genres, runtime=runtime,
            imdb_rating=rating, imdb_votes="200000", plot="История",
            poster_url="https://example.test/p.jpg", media_type=kind)

    async def test_series_never_reaches_the_movie_recommendation_flow(self):
        await self._film("tt900", "Сериал", kind="series", runtime="45 мин", rating="9.5")
        movie_id = await self._film("tt901", "Фильм", kind="movie")
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            ranked = await recommendations.ranked_candidates(
                1, partner_id=None, weights={}, filters={}, context={}, minimum=1)
        self.assertEqual([m["id"] for m in ranked], [movie_id])

    async def test_provider_type_is_persisted(self):
        film_id = await self._film("tt902", "Мультсериал", kind="series", runtime="22 мин")
        stored = await db.get_film(film_id)
        self.assertEqual(stored["media_type"], "series")
        self.assertEqual(stored["media_type_source"], "provider")

    async def test_pool_is_not_emptied_when_nothing_is_classified(self):
        """Если тип не проставлен вообще ни у чего, показываем прежний пул, а не
        пустой экран."""
        await self._film("tt903", "Без типа", kind=None)
        with patch("recommendations._warm_catalog_if_sparse", new=_keep_local_catalog):
            ranked = await recommendations.ranked_candidates(
                1, partner_id=None, weights={}, filters={}, context={}, minimum=1)
        self.assertEqual(len(ranked), 1)


if __name__ == "__main__":
    unittest.main()
