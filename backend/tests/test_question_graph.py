"""Граф анкеты: каждый вариант ответа обязан что-то менять.

Вариант, который не влияет ни на что, — это обман: человек выбирает разное и
получает одно и то же. Аудит перед этой правкой нашёл 23 таких варианта из 85.
"""
import unittest

import mood
from recommendation_graph import INTENTIONALLY_NEUTRAL, build_report, validate_question_graph
from recommendation_questions import QUESTIONS, next_question_id, option_for


class QuestionGraphTests(unittest.TestCase):
    def setUp(self):
        self.report = build_report()

    def test_every_answer_has_a_measurable_effect(self):
        self.assertEqual(self.report.orphan_answers, [])

    def test_graph_has_no_structural_problems(self):
        self.assertEqual(validate_question_graph().problems, [])

    def test_every_question_is_reachable(self):
        self.assertEqual(self.report.unreachable_questions, [])

    def test_every_answer_is_localized_in_both_languages(self):
        self.assertEqual(self.report.missing_localization, [])

    def test_no_duplicate_answer_ids_inside_a_question(self):
        self.assertEqual(self.report.duplicate_answer_ids, [])

    def test_mood_contributions_use_known_dimensions(self):
        self.assertEqual(self.report.invalid_mood_dimensions, [])
        for (question_id, answer_id), contribution in mood.ANSWER_CONTRIBUTIONS.items():
            with self.subTest(question=question_id, answer=answer_id):
                self.assertIsNotNone(option_for(question_id, answer_id),
                                     "вклад настроения ссылается на несуществующий ответ")
                for dimension in contribution.targets:
                    self.assertIn(dimension, mood.MOOD_DIMENSIONS)

    def test_neutral_answers_are_an_explicit_closed_list(self):
        """«Мне неважно» — единственная законная причина не влиять ни на что."""
        for question_id, answer_id in INTENTIONALLY_NEUTRAL:
            with self.subTest(answer=f"{question_id}:{answer_id}"):
                self.assertIsNotNone(option_for(question_id, answer_id))

    def test_missing_tension_follow_up_fails_loudly(self):
        """Молчаливая подмена на вопрос про ужасы возвращала не тот вопрос."""
        with self.assertRaises(KeyError):
            next_question_id({"q1": "tension", "t1": "несуществующий"})

    def test_every_tension_branch_has_its_own_follow_up(self):
        for option in QUESTIONS["t1"]["options"]:
            with self.subTest(answer=option["id"]):
                self.assertIn(f"t2_{option['id']}", QUESTIONS)

    def test_each_root_branch_produces_exactly_eight_screens(self):
        for option in QUESTIONS["q1"]["options"]:
            with self.subTest(branch=option["id"]):
                answers, screens = {}, []
                while (question_id := next_question_id(answers)) is not None:
                    screens.append(question_id)
                    answers[question_id] = (option["id"] if question_id == "q1"
                                            else QUESTIONS[question_id]["options"][0]["id"])
                    self.assertLessEqual(len(screens), 12, "граф зациклился")
                self.assertEqual(len(screens), 8)

    def test_specialized_tension_routes_are_all_walkable(self):
        for option in QUESTIONS["t1"]["options"]:
            with self.subTest(route=option["id"]):
                answers = {"q1": "tension", "t1": option["id"]}
                self.assertEqual(next_question_id(answers), f"t2_{option['id']}")


class AnswerEffectContentTests(unittest.TestCase):
    """Проверяем, что конкретные прежде-пустые ответы действительно ожили."""

    def _dimensions(self, question_id, answer_id):
        contribution = mood.ANSWER_CONTRIBUTIONS.get((question_id, answer_id))
        return set(contribution.targets) if contribution else set()

    def test_tension_follow_ups_now_change_the_request(self):
        for question_id in ("t2_psychological", "t2_mystery", "t2_survival", "t2_horror"):
            for option in QUESTIONS[question_id]["options"]:
                with self.subTest(answer=f"{question_id}:{option['id']}"):
                    self.assertTrue(self._dimensions(question_id, option["id"]))

    def test_different_answers_to_one_question_differ(self):
        """Иначе вариантов формально четыре, а смысл один."""
        for question_id in ("t2_psychological", "t2_mystery", "t2_survival", "t2_horror", "h3"):
            targets = []
            for option in QUESTIONS[question_id]["options"]:
                contribution = mood.ANSWER_CONTRIBUTIONS.get((question_id, option["id"]))
                targets.append(tuple(sorted(contribution.targets.items())) if contribution else ())
            with self.subTest(question=question_id):
                self.assertEqual(len(set(targets)), len(targets))

    def test_humor_pace_question_replaced_unmeasurable_archetypes(self):
        answer_ids = {option["id"] for option in QUESTIONS["h3"]["options"]}
        self.assertEqual(answer_ids, {"calm", "steady", "wild"})
        self.assertLess(mood.ANSWER_CONTRIBUTIONS[("h3", "calm")].targets["pace"],
                        mood.ANSWER_CONTRIBUTIONS[("h3", "wild")].targets["pace"])


if __name__ == "__main__":
    unittest.main()
