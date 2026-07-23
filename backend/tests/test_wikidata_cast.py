import unittest

import wikidata


class WikidataCastTests(unittest.TestCase):
    def test_commons_url_is_constrained_and_resized(self):
        url = wikidata.commons_thumbnail_url(
            "http://commons.wikimedia.org/wiki/Special:FilePath/Tobey Maguire 2012.jpg"
        )
        self.assertEqual(
            url,
            "https://commons.wikimedia.org/wiki/Special:FilePath/Tobey%20Maguire%202012.jpg?width=360",
        )
        self.assertIsNone(wikidata.commons_thumbnail_url("https://example.test/actor.jpg"))

    def test_cast_parser_orders_deduplicates_and_keeps_missing_photos_explicit(self):
        rows = [
            {
                "imdb": {"value": "tt0765010"}, "actor": {"value": "https://www.wikidata.org/entity/Q2"},
                "actorLabel": {"value": "Джейк Джилленхол"}, "ordinal": {"value": "2"},
            },
            {
                "imdb": {"value": "tt0765010"}, "actor": {"value": "https://www.wikidata.org/entity/Q1"},
                "actorLabel": {"value": "Тоби Магуайр"}, "ordinal": {"value": "1"},
                "image": {"value": "https://commons.wikimedia.org/wiki/Special:FilePath/Tobey.jpg"},
            },
            {  # duplicate Q1 from a second statement must not render twice
                "imdb": {"value": "tt0765010"}, "actor": {"value": "https://www.wikidata.org/entity/Q1"},
                "actorLabel": {"value": "Тоби Магуайр"}, "ordinal": {"value": "1"},
            },
        ]

        cast = wikidata._cast_from_bindings(rows, max_actors=10)["tt0765010"]

        self.assertEqual([person["name"] for person in cast], ["Тоби Магуайр", "Джейк Джилленхол"])
        self.assertIn("commons.wikimedia.org", cast[0]["photo_url"])
        self.assertIsNone(cast[1]["photo_url"])
