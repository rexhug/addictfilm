"""«Чёрный юмор» — составное требование, а не «в фильме есть комедия».

Отсюда живые дефекты продакшена: на запрос чёрного юмора выдавались семейный
мультсериал и лёгкий бадди-муви «К-9: Собачья работа». Проверяем СВОЙСТВА —
чтобы правило нельзя было удовлетворить одним лишь наличием юмора, абсурда или
криминальной линии.
"""
import unittest

import mood
import reasons
import recommendations as rec

ANSWERS = {"q1": "humor", "h1": "dark"}


def _film(film_id, title, *, genres, plot="", runtime="110 мин"):
    return {"id": film_id, "title": title, "genres": genres, "plot": plot,
            "runtime": runtime, "imdb_rating": "7.4", "imdb_votes": "150000"}


def _candidate(film, intent, *, quality=7.4, base=40.0, novelty=2.0):
    features = mood.build_movie_features(film)
    raw = mood.mood_similarity(intent, features)
    return {**film, "_mood_features": features, "_quality": quality, "_score": base,
            "_novelty": novelty, "_affinity": 0.0,
            "_mood": mood.confidence_adjusted(raw, features.confidence)}


class RequirementTests(unittest.TestCase):
    def setUp(self):
        self.intent = mood.build_mood_intent(ANSWERS)
        self.floors = mood.collect_dimension_floors(ANSWERS)
        self.requirements = mood.collect_compound_requirements(ANSWERS)

    def _eligible(self, film, **kwargs):
        candidate = _candidate(film, self.intent, **kwargs)
        return rec.mood_eligible([candidate], self.intent, self.floors, "best",
                                 requirements=self.requirements)

    def test_dark_humor_is_a_compound_requirement(self):
        self.assertTrue(self.requirements)
        self.assertEqual(self.requirements[0].code, "DARK_HUMOR")

    def test_genuine_dark_comedy_passes(self):
        real = _film(1, "Мрачная сатира", genres="комедия, криминал",
                     plot="Чёрная комедия о похоронном бюро и жадных наследниках.")
        self.assertTrue(self._eligible(real))

    def test_generic_action_comedy_is_rejected(self):
        """Ровно случай «К-9»: юмор есть, преступники есть — чёрной комедии нет."""
        buddy_cop = _film(2, "Собачья работа", genres="боевик, комедия, криминал",
                          plot="Весёлый полицейский и служебный пёс ловят наркодилера.")
        self.assertEqual(self._eligible(buddy_cop), [])

    def test_light_family_animation_is_rejected(self):
        cartoon = _film(3, "Робот-подросток", genres="мультфильм, комедия, семейный, приключения",
                        plot="Приключения девочки-робота, которая охраняет планету.")
        self.assertEqual(self._eligible(cartoon), [])

    def test_absurdity_alone_is_not_evidence(self):
        absurd = _film(4, "Хаос", genres="комедия",
                       plot="Абсурдная история про нелепые совпадения на свадьбе.")
        self.assertEqual(self._eligible(absurd), [])

    def test_high_base_score_cannot_buy_dark_humor(self):
        blockbuster = _film(5, "Блокбастер", genres="фантастика, боевик, комедия, приключения")
        self.assertEqual(self._eligible(blockbuster, base=89.0, quality=9.6), [])

    def test_word_in_a_title_is_not_dark_comedy_evidence(self):
        """Подстрока «черн» раньше делала «Чернику» чёрной комедией."""
        for title in ("Черника", "Черноокая Сьюзан", "Чёрная птица"):
            with self.subTest(title=title):
                self.assertNotIn("dark_humor",
                                 mood.build_movie_features(_film(6, title, genres="комедия")).markers)

    def test_satire_counts_only_with_a_genuinely_dark_tone(self):
        light_satire = _film(7, "Сатира", genres="комедия",
                             plot="Сатира на офисные нравы, всё заканчивается хорошо.")
        dark_satire = _film(8, "Мрачная сатира", genres="комедия, военный",
                            plot="Сатира на генералов, готовых уничтожить мир.")
        self.assertEqual(self._eligible(light_satire), [])
        self.assertTrue(self._eligible(dark_satire))

    def test_requirement_holds_on_every_fallback_level(self):
        buddy_cop = _film(9, "Собачья работа", genres="боевик, комедия, криминал")
        candidate = _candidate(buddy_cop, self.intent, base=88.0, quality=9.4)
        for level in mood.FALLBACK_THRESHOLD_ADJUSTMENTS:
            with self.subTest(level=level):
                self.assertEqual(rec.mood_eligible([candidate], self.intent, self.floors, "best",
                                                   fallback_level=level,
                                                   requirements=self.requirements), [])

    def test_every_role_keeps_the_same_requirement(self):
        cartoon = _candidate(_film(10, "Мультик", genres="мультфильм, комедия, семейный"), self.intent)
        for role in ("best", "reliable", "unexpected"):
            with self.subTest(role=role):
                self.assertEqual(rec.mood_eligible([cartoon], self.intent, self.floors, role,
                                                   requirements=self.requirements), [])

    def test_best_role_prefers_confirmed_evidence_over_higher_score(self):
        confirmed = _candidate(_film(11, "Трагикомедия", genres="комедия, драма, криминал",
                                     plot="Трагикомедия о неудачном ограблении."), self.intent,
                               base=30.0, quality=6.9)
        supported = _candidate(_film(12, "Мрачная сатира", genres="комедия, военный",
                                     plot="Сатира о войне, которую никто не хотел."), self.intent,
                               base=88.0, quality=9.2)
        pool = [{**m, "_best": m["_score"] / 90.0} for m in (confirmed, supported)]
        pick, _ = rec.select_mood_role(pool, self.intent, self.floors, "best", [], "_best",
                                       requirements=self.requirements, evidence_first=True)
        self.assertIsNotNone(pick)
        self.assertEqual(pick["id"], 11)


class ReasonTests(unittest.TestCase):
    def setUp(self):
        self.intent = mood.build_mood_intent(ANSWERS)
        self.requirements = mood.collect_compound_requirements(ANSWERS)

    def test_dark_comedy_gets_its_own_reason(self):
        film = _film(1, "Трагикомедия", genres="комедия, криминал",
                     plot="Чёрная комедия о наследстве.")
        codes = rec.build_reasons(_candidate(film, self.intent), intent=self.intent,
                                  requirements=self.requirements)
        self.assertIn(reasons.DARK_COMEDY_TONE, codes)

    def test_reasons_are_known_codes_only(self):
        film = _film(2, "Комедия", genres="комедия")
        codes = rec.build_reasons(_candidate(film, self.intent), intent=self.intent,
                                  requirements=self.requirements)
        self.assertTrue(set(codes) <= reasons.KNOWN_CODES)
        self.assertLessEqual(len(codes), reasons.MAX_REASONS)

    def test_internal_tags_never_leak_to_a_client(self):
        """Раньше наружу уходило «юмор, абсурд, сатира, a flawed lead»."""
        movie = _candidate(_film(3, "Комедия", genres="комедия"), self.intent)
        public = rec.public_movie(movie, role="best", language="ru",
                                  codes=["a flawed lead", "dark_humor", reasons.HIGH_QUALITY])
        self.assertEqual(public["reasons"], [reasons.HIGH_QUALITY])
        self.assertNotIn("flawed", public["explanation"])
        self.assertNotIn("dark_humor", public["explanation"])

    def test_explanation_follows_the_requested_language(self):
        movie = _candidate(_film(4, "Комедия", genres="комедия"), self.intent)
        russian = rec.public_movie(movie, role="best", codes=[reasons.HIGH_QUALITY], language="ru")
        english = rec.public_movie(movie, role="best", codes=[reasons.HIGH_QUALITY], language="en")
        self.assertEqual(russian["explanation"], "Высокая оценка зрителей")
        self.assertEqual(english["explanation"], "Highly rated by viewers")

    def test_every_known_code_has_both_languages(self):
        for code in reasons.KNOWN_CODES:
            with self.subTest(code=code):
                self.assertTrue(reasons.TEXTS[code]["ru"])
                self.assertTrue(reasons.TEXTS[code]["en"])


if __name__ == "__main__":
    unittest.main()
