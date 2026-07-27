"""Строгая проверка графа анкеты и отчёт о влиянии каждого ответа.

Смысл: вариант ответа, который ни на что не влияет, — это обман. Человек его
выбирает, ждёт другого результата и получает тот же. Такой вариант обязан либо
получить измеримый эффект, либо исчезнуть, и проверка здесь не даёт ему тихо
появиться снова.

Отчёт машиночитаемый: он же используется тестом в CI и админской диагностикой.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import mood
from recommendation_questions import BRANCH_STEPS, COMMON_STEPS, QUESTION_VERSION, QUESTIONS, next_question_id

# Варианты, у которых ОТСУТСТВИЕ эффекта и есть смысл: человек прямо говорит
# «мне неважно». Такой ответ обязан ничего не ограничивать, и требовать от него
# влияния было бы неверно. Список закрытый — новый вариант сюда просто так не
# попадёт, ему придётся получить настоящий эффект.
INTENTIONALLY_NEUTRAL: frozenset[tuple[str, str]] = frozenset({
    ("c1", "any"),      # «Время не важно»
    ("c2", "any"),      # «Неважно, главное — хороший»
    ("e3", "balance"),  # «Баланс героев и сюжета»
})


@dataclass(frozen=True)
class AnswerEffect:
    """Чем именно ответ меняет подбор. Пустой эффект — дефект анкеты."""
    question_id: str
    answer_id: str
    branch: str
    route_reachable: bool
    localized_ru: bool
    localized_en: bool
    hard_constraints: tuple[str, ...] = ()
    mood_dimensions: tuple[str, ...] = ()
    dimension_floors: tuple[str, ...] = ()
    compound_requirements: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    weights: tuple[str, ...] = ()

    @property
    def effects(self) -> tuple[str, ...]:
        """Только то, что реально меняет ВЫДАЧУ.

        `weights` намеренно не считается эффектом сам по себе: это устаревший
        слой тегов, и вариант, который умеет только двигать вес, но не влияет ни
        на настроение, ни на фильтры, ни на пороги, для человека неотличим от
        соседнего.
        """
        found = []
        for name in ("hard_constraints", "mood_dimensions", "dimension_floors",
                     "compound_requirements", "filters", "context"):
            if getattr(self, name):
                found.append(name)
        return tuple(found)

    @property
    def intentionally_neutral(self) -> bool:
        return (self.question_id, self.answer_id) in INTENTIONALLY_NEUTRAL

    @property
    def effective(self) -> bool:
        """Либо ответ что-то меняет, либо он честно объявлен «мне неважно»."""
        return bool(self.effects) or self.intentionally_neutral

    def as_dict(self) -> dict:
        return {
            "question_id": self.question_id, "answer_id": self.answer_id,
            "branch": self.branch, "route_reachable": self.route_reachable,
            "localized_ru": self.localized_ru, "localized_en": self.localized_en,
            "hard_constraints": list(self.hard_constraints),
            "mood_dimensions": list(self.mood_dimensions),
            "dimension_floors": list(self.dimension_floors),
            "compound_requirements": list(self.compound_requirements),
            "filters": list(self.filters), "context": list(self.context),
            "weights": list(self.weights),
            "effects": list(self.effects), "effective": self.effective,
            "intentionally_neutral": self.intentionally_neutral,
        }


@dataclass
class QuestionGraphReport:
    question_version: str = QUESTION_VERSION
    answers: list[AnswerEffect] = field(default_factory=list)
    unreachable_questions: list[str] = field(default_factory=list)
    unknown_branches: list[str] = field(default_factory=list)
    duplicate_answer_ids: list[str] = field(default_factory=list)
    missing_localization: list[str] = field(default_factory=list)
    invalid_mood_dimensions: list[str] = field(default_factory=list)
    invalid_routes: list[str] = field(default_factory=list)

    @property
    def orphan_answers(self) -> list[str]:
        return [f"{item.question_id}:{item.answer_id}" for item in self.answers
                if not item.effective]

    @property
    def problems(self) -> list[str]:
        return (self.unreachable_questions + self.unknown_branches
                + self.duplicate_answer_ids + self.missing_localization
                + self.invalid_mood_dimensions + self.invalid_routes
                + self.orphan_answers)

    def as_dict(self) -> dict:
        return {
            "question_version": self.question_version,
            "questions": len(QUESTIONS),
            "answers": len(self.answers),
            "orphan_answers": self.orphan_answers,
            "unreachable_questions": self.unreachable_questions,
            "unknown_branches": self.unknown_branches,
            "duplicate_answer_ids": self.duplicate_answer_ids,
            "missing_localization": self.missing_localization,
            "invalid_mood_dimensions": self.invalid_mood_dimensions,
            "invalid_routes": self.invalid_routes,
        }


def _reachable_questions() -> tuple[set[str], list[str]]:
    """Проходим граф так же, как это делает сервер, а не «как задумано»."""
    reachable = {"q1"}
    invalid: list[str] = []
    for answer in QUESTIONS["q1"]["options"]:
        branch = answer["id"]
        if branch not in BRANCH_STEPS:
            continue
        for step in BRANCH_STEPS[branch]:
            if step != "__t2__":
                reachable.add(step)
                continue
            # Специализированное продолжение ветки напряжения: для КАЖДОГО
            # ответа t1 должен существовать свой вопрос. Раньше отсутствующий
            # молча подменялся вопросом про ужасы — человек получал не то, что
            # выбирал, и заметить это было нечем.
            for t1_answer in QUESTIONS["t1"]["options"]:
                follow_up = f"t2_{t1_answer['id']}"
                if follow_up in QUESTIONS:
                    reachable.add(follow_up)
                else:
                    invalid.append(f"t1:{t1_answer['id']} → отсутствует {follow_up}")
    reachable.update(COMMON_STEPS)
    return reachable, invalid


def _mood_effect(question_id: str, answer_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    contribution = mood.ANSWER_CONTRIBUTIONS.get((question_id, answer_id))
    dimensions = tuple(sorted(contribution.targets)) if contribution else ()
    invalid = tuple(dim for dim in dimensions if dim not in mood.MOOD_DIMENSIONS)
    return dimensions, invalid


def build_report() -> QuestionGraphReport:
    report = QuestionGraphReport()
    reachable, invalid_routes = _reachable_questions()
    report.invalid_routes = invalid_routes

    for question_id, question in QUESTIONS.items():
        if question_id not in reachable:
            report.unreachable_questions.append(question_id)
        branch = question.get("branch", "")
        if branch not in {*BRANCH_STEPS, "start", "common"}:
            report.unknown_branches.append(f"{question_id}:{branch}")

        seen: set[str] = set()
        for option in question["options"]:
            answer_id = option["id"]
            if answer_id in seen:
                report.duplicate_answer_ids.append(f"{question_id}:{answer_id}")
            seen.add(answer_id)

            label = option.get("label") or {}
            has_ru, has_en = bool(label.get("ru")), bool(label.get("en"))
            if not has_ru or not has_en:
                report.missing_localization.append(f"{question_id}:{answer_id}")

            dimensions, invalid = _mood_effect(question_id, answer_id)
            report.invalid_mood_dimensions.extend(
                f"{question_id}:{answer_id}:{dim}" for dim in invalid)

            floors = tuple(sorted(mood.DIMENSION_FLOORS.get((question_id, answer_id), {})))
            compound = tuple(
                requirement.code
                for key, requirement in mood.COMPOUND_REQUIREMENTS.items()
                if key == (question_id, answer_id))
            hard = tuple(sorted(mood.DURATION_CONSTRAINTS.get(answer_id, {}))) \
                if question_id == "c1" else ()

            report.answers.append(AnswerEffect(
                question_id=question_id, answer_id=answer_id, branch=branch,
                route_reachable=question_id in reachable,
                localized_ru=has_ru, localized_en=has_en,
                hard_constraints=hard,
                mood_dimensions=dimensions,
                dimension_floors=floors,
                compound_requirements=compound,
                filters=tuple(sorted(option.get("filters") or {})),
                context=tuple(sorted(option.get("context") or {})),
                weights=tuple(sorted(option.get("weights") or {})),
            ))
    return report


def validate_question_graph() -> QuestionGraphReport:
    """Отчёт для тестов и диагностики. Бросать исключение здесь не нужно —
    падать должен тест, показывая ВЕСЬ список проблем сразу."""
    return build_report()


def assert_valid_question_graph() -> None:
    report = validate_question_graph()
    if report.problems:
        raise ValueError("Граф анкеты некорректен: " + "; ".join(report.problems))


def coverage_rows(report: QuestionGraphReport | None = None) -> list[dict]:
    return [item.as_dict() for item in (report or build_report()).answers]


def _next_question_for(answers: dict) -> str | None:
    return next_question_id(answers)


def walk_branch(branch_answer: str, picks: dict | None = None) -> list[str]:
    """Пройти ветку до конца ровно тем же кодом, что и сервер."""
    answers: dict[str, str] = {}
    picks = picks or {}
    visited: list[str] = []
    while True:
        question_id = _next_question_for(answers)
        if question_id is None:
            return visited
        visited.append(question_id)
        question = QUESTIONS[question_id]
        if question_id == "q1":
            answers[question_id] = branch_answer
        else:
            answers[question_id] = picks.get(question_id, question["options"][0]["id"])
        if len(visited) > 20:      # защита от цикла в конфигурации
            return visited
