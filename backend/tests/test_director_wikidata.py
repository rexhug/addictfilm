import unittest

import wikidata


class WikidataDirectorParserTests(unittest.TestCase):
    def test_director_bindings_keep_portrait_and_remove_duplicates(self):
        rows = [
            {
                "imdb": {"value": "tt0133093"},
                "director": {"value": "http://www.wikidata.org/entity/Q123"},
                "directorLabel": {"value": "Лана Вачовски"},
                "image": {"value": "https://commons.wikimedia.org/wiki/Special:FilePath/Lana.jpg"},
            },
            {
                "imdb": {"value": "tt0133093"},
                "director": {"value": "http://www.wikidata.org/entity/Q123"},
                "directorLabel": {"value": "Лана Вачовски"},
            },
        ]

        self.assertEqual(wikidata._directors_from_bindings(rows), {
            "tt0133093": [{
                "name": "Лана Вачовски",
                "photo_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Lana.jpg?width=330",
                "source": "wikidata",
            }],
        })
