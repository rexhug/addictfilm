import unittest

import kinopoisk


class KinopoiskPeoplePhotoTests(unittest.TestCase):
    def test_director_portraits_keep_only_directors_in_credit_order(self):
        people = [
            {"name": "Actor", "enProfession": "actor", "photo": "https://st.kp.yandex.net/actor.jpg"},
            {"name": "Director One", "enProfession": "director", "photo": "https://st.kp.yandex.net/one.jpg"},
            {"name": "Director Two", "enProfession": "director", "photo": "https://st.kp.yandex.net/two.jpg"},
            {"name": "Director Three", "enProfession": "director", "photo": "https://st.kp.yandex.net/three.jpg"},
        ]

        self.assertEqual(kinopoisk.extract_director_photos(people), [
            {"name": "Director One", "photo_url": "https://st.kp.yandex.net/one.jpg", "source": "kinopoisk"},
            {"name": "Director Two", "photo_url": "https://st.kp.yandex.net/two.jpg", "source": "kinopoisk"},
        ])
