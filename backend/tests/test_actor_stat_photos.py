import json
import tempfile
import unittest
from pathlib import Path

import database as db


class ActorStatPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": None})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def test_favorite_actor_keeps_a_photo_from_watched_films(self):
        photo_url = "https://st.kp.yandex.net/images/actor.jpg"
        actors_photos = json.dumps([{"name": "Alex Actor", "photo_url": photo_url}])
        for index in (1, 2):
            film_id = await db.get_or_create_film(
                f"tt000000{index}", f"Film {index}", actors="Alex Actor",
                runtime="100 min", actors_photos=actors_photos)
            await db.set_status(1, film_id, "watched")

        solo_film = await db.get_or_create_film(
            "tt0000003", "Solo film", actors="Solo Actor", runtime="100 min",
        )
        await db.set_status(1, solo_film, "watched")

        stats = await db.get_user_stats(1)

        self.assertEqual(stats["top_actors"], [
            ("Alex Actor", 2, photo_url),
            ("Solo Actor", 1, None),
        ])

    async def test_person_drilldown_uses_an_exact_name_and_role(self):
        wanted = await db.get_or_create_film(
            "tt0000010", "Exact", actors="Alex Actor", directors="Alex Director", runtime="100 min",
        )
        similarly_named = await db.get_or_create_film(
            "tt0000011", "Different", actors="Alexandra Actor", directors="Another Director", runtime="100 min",
        )
        await db.set_status(1, wanted, "watched")
        await db.set_status(1, similarly_named, "watched")

        actor_films = await db.get_person_watched_films(1, "actor", "Alex Actor")
        director_films = await db.get_person_watched_films(1, "director", "Alex Director")

        self.assertEqual([film["id"] for film in actor_films], [wanted])
        self.assertEqual([film["id"] for film in director_films], [wanted])

    async def test_wikidata_cast_replaces_weak_source_once_and_is_not_requeued(self):
        film_id = await db.get_or_create_film(
            "tt0765010", "Братья", actors="Случайный актёр",
            actors_photos=json.dumps([{"name": "Случайный актёр", "photo_url": "https://st.kp.yandex.net/k.jpg"}]),
        )
        cast = [
            {"name": "Тоби Магуайр", "photo_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Tobey.jpg", "source": "wikidata"},
            {"name": "Джейк Джилленхол", "photo_url": None, "source": "wikidata"},
        ]

        self.assertTrue(await db.set_film_cast_from_wikidata(
            film_id, ", ".join(person["name"] for person in cast), json.dumps(cast, ensure_ascii=False),
        ))
        stored = await db.get_film(film_id)

        self.assertEqual(stored["actors"], "Тоби Магуайр, Джейк Джилленхол")
        self.assertEqual(json.loads(stored["actors_photos"]), cast)
        self.assertIsNotNone(stored["actor_photos_checked_at"])
        self.assertEqual(await db.films_needing_actor_photo_enrichment(), [])

    async def test_favorite_director_keeps_a_portrait_from_watched_films(self):
        photo_url = "https://commons.wikimedia.org/wiki/Special:FilePath/Director.jpg?width=360"
        directors = [{"name": "Alex Director", "photo_url": photo_url, "source": "wikidata"}]
        for index in (3, 4):
            film_id = await db.get_or_create_film(
                f"tt000000{index}", f"Film {index}", directors="Alex Director", runtime="100 min")
            await db.set_film_directors_from_wikidata(
                film_id, "Alex Director", json.dumps(directors, ensure_ascii=False),
            )
            await db.set_status(1, film_id, "watched")

        single_film = await db.get_or_create_film(
            "tt0000005", "Film 5", directors="Solo Director", runtime="100 min")
        await db.set_film_directors_from_wikidata(
            single_film, "Solo Director", json.dumps([{"name": "Solo Director", "photo_url": None}], ensure_ascii=False),
        )
        await db.set_status(1, single_film, "watched")

        stats = await db.get_user_stats(1)

        self.assertEqual(stats["top_directors"], [
            ("Alex Director", 2, photo_url),
            ("Solo Director", 1, None),
        ])
