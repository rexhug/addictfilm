"""Сценарии оценки должны оставаться проходимыми и покрывать все ветки.

Сам прогон идёт на реальном каталоге вне тестов; здесь проверяется, что набор
сценариев не разъехался с анкетой — иначе базовая линия молча перестанет что-то
измерять.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))

from evaluate_recommendations import PROFILES, PROPERTY_CHECKS, SCENARIOS, _validate_answers
from recommendation_questions import QUESTIONS


class ScenarioCoverageTests(unittest.TestCase):
    def test_at_least_thirty_full_scenarios(self):
        self.assertGreaterEqual(len(SCENARIOS), 30)

    def test_every_scenario_walks_the_real_question_graph(self):
        """Сценарий, который нельзя пройти в приложении, ничего не измеряет."""
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario["name"]):
                _validate_answers(scenario["answers"])

    def test_every_root_branch_is_covered(self):
        covered = {scenario["answers"]["q1"] for scenario in SCENARIOS}
        expected = {option["id"] for option in QUESTIONS["q1"]["options"]}
        self.assertEqual(covered, expected)

    def test_every_specialized_tension_branch_is_covered(self):
        covered = {scenario["answers"]["t1"] for scenario in SCENARIOS
                   if scenario["answers"].get("t1")}
        expected = {option["id"] for option in QUESTIONS["t1"]["options"]}
        self.assertEqual(covered, expected)

    def test_all_three_profiles_are_exercised(self):
        self.assertEqual({scenario["profile"] for scenario in SCENARIOS}, set(PROFILES))

    def test_scenario_names_are_unique(self):
        names = [scenario["name"] for scenario in SCENARIOS]
        self.assertEqual(len(names), len(set(names)))

    def test_expectations_reference_known_property_checks(self):
        for scenario in SCENARIOS:
            for expectation in scenario["expect"]:
                with self.subTest(scenario=scenario["name"], expect=expectation):
                    self.assertIn(expectation, PROPERTY_CHECKS)

    def test_incomplete_scenario_is_rejected_loudly(self):
        with self.assertRaises(SystemExit):
            _validate_answers({"q1": "humor"})

    def test_scenario_with_extra_answers_is_rejected(self):
        complete = dict(SCENARIOS[0]["answers"])
        complete["не-из-маршрута"] = "x"
        with self.assertRaises(SystemExit):
            _validate_answers(complete)

    def test_property_checks_are_real_predicates(self):
        """Проверка свойства обязана уметь и отвергать, а не только принимать."""
        import mood
        cozy = mood.build_movie_features({"id": 1, "title": "Уютное", "genres": "семейный",
                                          "plot": "", "runtime": "100 мин"})
        grim = mood.build_movie_features({"id": 2, "title": "Мрачное", "genres": "ужасы",
                                          "plot": "", "runtime": "100 мин"})
        self.assertTrue(PROPERTY_CHECKS["cozy"](cozy))
        self.assertFalse(PROPERTY_CHECKS["cozy"](grim))
        self.assertTrue(PROPERTY_CHECKS["tension"](grim))
        self.assertFalse(PROPERTY_CHECKS["tension"](cozy))


if __name__ == "__main__":
    unittest.main()
