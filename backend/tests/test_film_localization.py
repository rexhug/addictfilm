"""Язык метаданных каталога.

Главное обещание проверки — не «функция считает как написано», а продуктовое:
русский интерфейс не показывает английский жанр и украинское описание, и ни
один провайдер не может затереть локализацию, сделанную другим.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import database as db
import film_localization as flang
import search

RU_PLOT = "Сотрудник страховой компании страдает бессонницей."
UA_PLOT = "Працівник страхової компанії страждає на безсоння."
EN_PLOT = "An insomniac office worker forms an underground fight club."


class LanguageDetectionTests(unittest.TestCase):
    def test_ukrainian_is_never_accepted_as_russian(self):
        """Ровно тот баг: украинское описание в русской карточке.

        Отличается оно только буквами і/ї/є/ґ — и этого достаточно."""
        self.assertTrue(flang.looks_ukrainian(UA_PLOT))
        self.assertFalse(flang.looks_russian(UA_PLOT))
        self.assertTrue(flang.looks_russian(RU_PLOT))

    def test_latin_text_is_not_russian_and_cyrillic_is_not_english(self):
        self.assertFalse(flang.looks_russian(EN_PLOT))
        self.assertTrue(flang.looks_english(EN_PLOT))
        self.assertFalse(flang.looks_english(RU_PLOT))

    def test_empty_values_belong_to_no_language(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertFalse(flang.looks_russian(value))
                self.assertFalse(flang.looks_english(value))

    def test_unknown_language_code_falls_back_to_russian(self):
        for value in (None, "", "uk", "de", "EN-us"):
            with self.subTest(value=value):
                self.assertIn(flang.normalize_language(value), flang.SUPPORTED_LANGUAGES)
        self.assertEqual(flang.normalize_language("uk"), "ru")
        self.assertEqual(flang.normalize_language("EN"), "en")


class GenreDisplayTests(unittest.TestCase):
    def test_one_canonical_genre_renders_in_both_languages(self):
        self.assertEqual(flang.localized_genres("криминал, драма, триллер", "ru"),
                         "криминал, драма, триллер")
        self.assertEqual(flang.localized_genres("криминал, драма, триллер", "en"),
                         "Crime, Drama, Thriller")

    def test_english_provider_genres_also_render_in_russian(self):
        self.assertEqual(flang.localized_genres("Crime, Drama, Thriller", "ru"),
                         "криминал, драма, триллер")
        self.assertEqual(flang.localized_genres("Crime, Drama, Thriller", "en"),
                         "Crime, Drama, Thriller")

    def test_a_mixed_legacy_string_does_not_show_one_genre_twice(self):
        self.assertEqual(flang.localized_genres("Drama, драма", "ru"), "драма")

    def test_an_unknown_genre_survives_instead_of_dropping_the_film(self):
        self.assertEqual(flang.localized_genres("драма, зомби-мюзикл", "en"),
                         "Drama, зомби-мюзикл")

    def test_every_showcase_genre_has_an_english_label(self):
        from db_helpers import _GENRE_SHOWCASE
        missing = sorted(g for g in _GENRE_SHOWCASE if g not in flang.GENRE_DISPLAY_EN)
        self.assertEqual(missing, [], "жанр витрины без английской подписи")


class ProviderSlotTests(unittest.TestCase):
    """Провайдер пишет только в СВОИ языковые слоты."""

    def test_kinopoisk_fills_russian_slots(self):
        details = search._kp_details({
            "id": 361, "name": "Бойцовский клуб", "alternativeName": "Fight Club",
            "year": 1999, "description": RU_PLOT,
            "genres": [{"name": "триллер"}, {"name": "драма"}],
            "persons": [{"name": "Дэвид Финчер", "enName": "David Fincher",
                         "enProfession": "director"}],
        })
        self.assertEqual(details["title_ru"], "Бойцовский клуб")
        self.assertEqual(details["title_en"], "Fight Club")
        self.assertEqual(details["plot_ru"], RU_PLOT)
        self.assertEqual(details["directors_ru"], "Дэвид Финчер")
        self.assertEqual(details["directors_en"], "David Fincher")
        self.assertEqual(details["genres_en"], "Thriller, Drama")
        self.assertNotIn("plot_en", details)

    def test_omdb_fills_english_slots_only(self):
        details = search._omdb_catalog_details(
            {"imdbID": "tt0137523", "Title": "Fight Club", "Plot": EN_PLOT,
             "Genre": "Crime, Drama, Thriller", "Director": "David Fincher"},
            "Fight Club", None)
        self.assertEqual(details["plot_en"], EN_PLOT)
        self.assertEqual(details["directors_en"], "David Fincher")
        self.assertIsNone(details["title_ru"])
        self.assertNotIn("plot_ru", details)
        # Жанры детерминированы и потому доступны на обоих языках без запроса.
        self.assertEqual(details["genres_ru"], "криминал, драма, триллер")


class CatalogWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp.name) / "loc.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp.cleanup()

    async def test_english_provider_never_overwrites_russian_slots(self):
        """Главный инвариант всей задачи."""
        film_id = await db.get_or_create_film(
            imdb_id="tt0137523", title="Бойцовский клуб", title_original="Fight Club",
            title_ru="Бойцовский клуб", plot_ru=RU_PLOT, directors_ru="Дэвид Финчер",
            genres="триллер", media_type="movie")
        await db.get_or_create_film(
            imdb_id="tt0137523", title="Fight Club", title_en="Fight Club",
            plot_en=EN_PLOT, directors_en="David Fincher", media_type="movie")
        film = await db.get_film(film_id)
        self.assertEqual(film["plot_ru"], RU_PLOT)
        self.assertEqual(film["directors_ru"], "Дэвид Финчер")
        self.assertEqual(film["plot_en"], EN_PLOT)
        self.assertEqual(film["directors_en"], "David Fincher")

    async def test_a_wrong_language_value_is_refused_at_the_door(self):
        film_id = await db.get_or_create_film(
            imdb_id="tt0137523", title="Fight Club",
            plot_ru=EN_PLOT, title_ru="Fight Club", media_type="movie")
        film = await db.get_film(film_id)
        self.assertIsNone(film["plot_ru"], "английский текст не может быть русским слотом")
        self.assertIsNone(film["title_ru"])

    async def test_search_text_carries_both_titles(self):
        await db.get_or_create_film(
            imdb_id="tt0137523", title="Бойцовский клуб", title_original="Fight Club",
            title_ru="Бойцовский клуб", title_en="Fight Club", media_type="movie")
        for query in ("бойцовский клуб", "fight club"):
            with self.subTest(query=query):
                self.assertTrue(await db.search_catalog(query), f"не найден по «{query}»")

    async def test_repair_replaces_a_ukrainian_russian_slot_but_keeps_good_ones(self):
        film_id = await db.get_or_create_film(
            imdb_id="tt0137523", title="Бойцовский клуб", media_type="movie")
        # Украинский текст мог попасть в слот только в обход загрузки — так и
        # было в проде, поэтому кладём его напрямую.
        async with db.db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute(
                "UPDATE films SET plot_ru = ?, directors_ru = ? WHERE id = ?",
                (UA_PLOT, "Дэвид Финчер", film_id))
            await conn.commit()
        replaced = await db.repair_film_localization(
            film_id, {"plot_ru": RU_PLOT, "directors_ru": "Хтось Інший"})
        film = await db.get_film(film_id)
        self.assertTrue(replaced)
        self.assertEqual(film["plot_ru"], RU_PLOT, "украинский слот обязан замениться")
        self.assertEqual(film["directors_ru"], "Дэвид Финчер",
                         "годный русский слот трогать нельзя")


class LegacySeedTests(unittest.TestCase):
    """Разбор строки в том виде, в каком она лежит в проде сейчас."""

    def test_a_fight_club_shaped_row_seeds_without_any_provider_call(self):
        import localization_backfill as backfill

        seeded = backfill.seed_from_catalog({
            "title": "Бойцовский клуб", "title_original": "Fight Club",
            "plot": UA_PLOT, "genres": "Crime, Drama, Thriller",
            "directors": "Дэвид Финчер",
        })
        self.assertEqual(seeded["title_ru"], "Бойцовский клуб")
        self.assertEqual(seeded["title_en"], "Fight Club")
        self.assertEqual(seeded["genres_ru"], "криминал, драма, триллер")
        self.assertEqual(seeded["genres_en"], "Crime, Drama, Thriller")
        self.assertEqual(seeded["directors_ru"], "Дэвид Финчер")
        self.assertNotIn("plot_ru", seeded, "украинский plot не должен попасть в RU")
        self.assertNotIn("plot_en", seeded)


if __name__ == "__main__":
    unittest.main()
