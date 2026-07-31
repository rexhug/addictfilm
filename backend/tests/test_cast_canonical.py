"""Состав фильма: порядок титров, устойчивая личность, портрет отдельно.

Главное обещание здесь одно: список актёров отражает ТИТРЫ ЭТОГО ФИЛЬМА. Ни
наличие фотографии, ни алфавит, ни общая известность, ни порядок съёмок из
Wikidata не имеют права его менять. Актёр без портрета остаётся на своём месте —
пропуск главного героя ради того, что у второстепенного есть фото, это неверные
данные, а не аккуратная выдача.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database as db
import kinopoisk
import main
import wikidata


def person(pid, name, *, profession="actor", photo=None, en=None, role=None) -> dict:
    return {"id": pid, "name": name, "enProfession": profession,
            "photo": photo, "enName": en, "description": role}


class CastExtractionTests(unittest.TestCase):
    def test_billing_order_follows_the_provider_not_the_photos(self):
        cast = kinopoisk.extract_cast([
            person(1, "Главный", photo=None),
            person(2, "Второй", photo="https://st.kp.yandex.net/2.jpg"),
            person(3, "Третий", photo="https://st.kp.yandex.net/3.jpg"),
        ])
        self.assertEqual([p["name"] for p in cast], ["Главный", "Второй", "Третий"])
        self.assertEqual([p["billing_order"] for p in cast], [0, 1, 2])
        # Главный герой без фотографии стоит впереди снятых второстепенных.
        self.assertIsNone(cast[0]["photo_url"])

    def test_identity_and_role_survive_extraction(self):
        cast = kinopoisk.extract_cast([
            person(42, "Джейк Джилленхол", en="Jake Gyllenhaal", role="Davis Mitchell",
                   photo="https://st.kp.yandex.net/j.jpg")])
        self.assertEqual(cast[0]["person_id"], "42")
        self.assertEqual(cast[0]["original_name"], "Jake Gyllenhaal")
        self.assertEqual(cast[0]["character"], "Davis Mitchell")
        self.assertEqual(cast[0]["source"], "kinopoisk")
        self.assertEqual(cast[0]["photo_state"], "unverified")
        self.assertEqual(cast[0]["fallback_photo_urls"], [])

    def test_only_actors_and_at_most_twelve(self):
        people = [person(0, "Режиссёр", profession="director")]
        people += [person(i, f"Актёр {i}") for i in range(1, 20)]
        cast = kinopoisk.extract_cast(people)
        self.assertEqual(len(cast), kinopoisk.MAX_CAST)
        self.assertEqual(cast[0]["name"], "Актёр 1")

    def test_a_nameless_entry_never_becomes_a_cast_member(self):
        self.assertEqual(kinopoisk.extract_cast([person(1, "  ")]), [])
        self.assertEqual(kinopoisk.extract_cast([]), [])
        self.assertEqual(kinopoisk.extract_cast(None), [])


class WikidataPortraitOnlyTests(unittest.TestCase):
    """Wikidata приносит ссылку на портрет и больше ничего."""

    def setUp(self):
        self.cast = [
            {"person_id": "1", "wikidata_id": None, "name": "Главный",
             "original_name": "Lead Actor", "character": "Герой", "billing_order": 0,
             "source": "kinopoisk", "photo_url": None, "fallback_photo_urls": []},
            {"person_id": "2", "wikidata_id": "Q2", "name": "Второй",
             "original_name": "Second Actor", "character": "Друг", "billing_order": 1,
             "source": "kinopoisk", "photo_url": "https://st.kp.yandex.net/2.jpg",
             "fallback_photo_urls": []},
        ]

    def test_it_cannot_reorder_rename_or_extend_the_cast(self):
        researched = [
            {"name": "Второй", "wikidata_id": "Q2", "photo_url": "https://commons.wikimedia.org/w.jpg"},
            {"name": "Посторонний", "wikidata_id": "Q9", "photo_url": "https://commons.wikimedia.org/x.jpg"},
        ]
        result = wikidata.portrait_candidates_for_cast(self.cast, researched)
        self.assertEqual([p["name"] for p in result], ["Главный", "Второй"])
        self.assertEqual([p["billing_order"] for p in result], [0, 1])
        self.assertEqual([p["character"] for p in result], ["Герой", "Друг"])
        self.assertTrue(all(p["source"] == "kinopoisk" for p in result))

    def test_a_stable_identifier_wins_over_a_name(self):
        researched = [{"name": "Совсем другое имя", "wikidata_id": "Q2",
                       "photo_url": "https://commons.wikimedia.org/w.jpg"}]
        result = wikidata.portrait_candidates_for_cast(self.cast, researched)
        self.assertEqual(result[1]["name"], "Второй")
        self.assertIn("https://commons.wikimedia.org/w.jpg",
                      [result[1]["photo_url"], *result[1]["fallback_photo_urls"]])

    def test_an_ambiguous_name_is_never_matched(self):
        researched = [
            {"name": "Главный", "photo_url": "https://commons.wikimedia.org/a.jpg"},
            {"name": "Главный", "photo_url": "https://commons.wikimedia.org/b.jpg"},
        ]
        result = wikidata.portrait_candidates_for_cast(self.cast, researched)
        self.assertIsNone(result[0]["photo_url"], "тёзки не должны сливаться")

    def test_an_exact_unique_name_provides_a_portrait(self):
        researched = [{"name": "Lead Actor", "photo_url": "https://commons.wikimedia.org/a.jpg"}]
        result = wikidata.portrait_candidates_for_cast(self.cast, researched)
        self.assertEqual(result[0]["photo_url"], "https://commons.wikimedia.org/a.jpg")

    def test_the_free_portrait_leads_and_the_catalog_one_becomes_a_fallback(self):
        # Это про лицензию картинки, а не про приоритет источника в составе:
        # порядок актёров при этом не меняется (проверено выше).
        researched = [{"name": "Второй", "wikidata_id": "Q2",
                       "photo_url": "https://commons.wikimedia.org/w.jpg"}]
        result = wikidata.portrait_candidates_for_cast(self.cast, researched)
        self.assertEqual(result[1]["photo_url"], "https://commons.wikimedia.org/w.jpg")
        self.assertEqual(result[1]["fallback_photo_urls"], ["https://st.kp.yandex.net/2.jpg"])
        self.assertEqual(result[1]["billing_order"], 1)


class CastStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "cast.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()
        await db.upsert_user({"id": 1, "first_name": "One", "username": "one"})

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def test_the_canonical_cast_is_stored_and_legacy_columns_stay(self):
        cast = kinopoisk.extract_cast([person(1, "Главный"), person(2, "Второй")])
        film_id = await db.get_or_create_film(
            "tt5000001", "Кино", actors="Главный, Второй",
            cast_json=json.dumps(cast, ensure_ascii=False))
        stored = await db.get_film(film_id)
        self.assertEqual([p["name"] for p in json.loads(stored["cast_json"])],
                         ["Главный", "Второй"])
        self.assertEqual(stored["actors"], "Главный, Второй")

    async def test_a_row_without_a_canonical_cast_still_works(self):
        film_id = await db.get_or_create_film("tt5000002", "Старое", actors="Кто-то")
        stored = await db.get_film(film_id)
        self.assertIsNone(stored["cast_json"])
        self.assertEqual(stored["actors"], "Кто-то")

    async def test_malformed_cast_json_never_reaches_the_client_as_a_crash(self):
        film_id = await db.get_or_create_film(
            "tt5000003", "Битое", actors="Кто-то", cast_json="{не json")
        with mock.patch.object(main, "CAST_V2_ENABLED", True):
            payload = await main.movie(film_id, user={"id": 1})
        # Сервер отдаёт строку как есть; разбирать её — задача клиента, и он
        # обязан переживать мусор. Здесь важно, что запрос не падает.
        self.assertEqual(payload["id"], film_id)

    async def test_the_flag_decides_whether_the_canonical_cast_is_exposed(self):
        cast = kinopoisk.extract_cast([person(1, "Главный")])
        film_id = await db.get_or_create_film(
            "tt5000004", "Кино", actors="Главный",
            cast_json=json.dumps(cast, ensure_ascii=False))
        with mock.patch.object(main, "CAST_V2_ENABLED", False):
            hidden = await main.movie(film_id, user={"id": 1})
        with mock.patch.object(main, "CAST_V2_ENABLED", True):
            shown = await main.movie(film_id, user={"id": 1})
        self.assertNotIn("cast_json", hidden)
        self.assertIn("cast_json", shown)
        self.assertNotIn("cast_checked_at", shown)

    async def test_opening_a_film_never_waits_for_an_external_lookup(self):
        """Карточка приходит из каталога. Обогащение идёт фоном, и запрос НЕ
        должен его дожидаться — иначе открытие фильма зависит от чужого сервиса.

        Проверяем это тем, что внешний вызов не отвечает никогда: если бы ответ
        его ждал, movie() не вернулся бы за отведённое время.
        """
        import asyncio

        film_id = await db.get_or_create_film(
            "tt5000005", "Кино", actors="Главный",
            actors_photos=json.dumps([{"name": "Главный", "photo_url": None}]))

        async def _never(*_args, **_kwargs):
            await asyncio.Event().wait()

        with mock.patch.object(main.wikidata, "get_cast_by_imdb", new=_never), \
             mock.patch.object(main.wikidata, "get_directors_by_imdb", new=_never):
            payload = await asyncio.wait_for(main.movie(film_id, user={"id": 1}), timeout=0.5)
        self.assertEqual(payload["id"], film_id)
        for task in list(main._people_enrichment_tasks):
            task.cancel()
        await asyncio.gather(*main._people_enrichment_tasks, return_exceptions=True)


class WikidataCannotReorderStoredCastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "order.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def test_a_lead_without_a_photo_keeps_its_place(self):
        film_id = await db.get_or_create_film(
            "tt6000001", "Кино", actors="Главный, Второй",
            actors_photos=json.dumps([
                {"name": "Главный", "photo_url": None},
                {"name": "Второй", "photo_url": "https://st.kp.yandex.net/2.jpg"},
            ], ensure_ascii=False))
        # Wikidata знает только второго и «своего» третьего.
        researched = [
            {"name": "Второй", "photo_url": "https://commons.wikimedia.org/w.jpg", "source": "wikidata"},
            {"name": "Третий", "photo_url": "https://commons.wikimedia.org/t.jpg", "source": "wikidata"},
        ]
        await db.set_film_cast_from_wikidata(
            film_id, ", ".join(p["name"] for p in researched),
            json.dumps(researched, ensure_ascii=False))
        stored = await db.get_film(film_id)
        self.assertEqual(stored["actors"], "Главный, Второй, Третий")
        merged = json.loads(stored["actors_photos"])
        self.assertEqual([p["name"] for p in merged], ["Главный", "Второй", "Третий"])
        self.assertIsNone(merged[0]["photo_url"], "актёр без фото не должен исчезать")
        self.assertEqual(merged[1]["photo_url"], "https://commons.wikimedia.org/w.jpg")
        self.assertEqual(merged[1]["fallback_photo_urls"], ["https://st.kp.yandex.net/2.jpg"])


if __name__ == "__main__":
    unittest.main()
