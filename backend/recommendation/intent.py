"""Канонический запрос пользователя — один контракт вместо четырёх параллельных.

До этого модуля смысл ответа жил в четырёх местах одновременно: веса вопросов,
легаси-теги фильма, измерения настроения и таксономия обогащения. Они не были
согласованы: анкета говорила `dark`, признаки — `dark_humor`, объяснение —
`DARK_COMEDY_TONE`, а профиль обогащения — `dark_comedy`.

RecommendationIntent сводит это к одному словарю (enrichment.taxonomy) и делает
явными три разные вещи, которые раньше путались:

* ОБЯЗАТЕЛЬНО (required)  — без этого фильм не подходит вообще;
* ЖЕЛАТЕЛЬНО (preferred)  — влияет на порядок, но не отсекает;
* НЕДОПУСТИМО (forbidden) — прямо противоречит запросу.

V1 не трогаем: он продолжает работать на прежней семантике до конца обкатки.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import mood
from enrichment.taxonomy import (
    MOVIE_TAXONOMY_VERSION,
    AudienceFlag,
    CanonicalGenre,
    CanonicalTheme,
    CanonicalTone,
)
from recommendation_questions import QUESTION_VERSION


@dataclass(frozen=True)
class IntentContribution:
    """Вклад одного ответа. Пустой вклад запрещён — см. тест покрытия."""
    required_features: frozenset[str] = frozenset()
    preferred_features: dict[str, float] = field(default_factory=dict)
    forbidden_features: frozenset[str] = frozenset()
    hard_constraints: dict[str, object] = field(default_factory=dict)
    exploration: float | None = None
    watch_context: str | None = None


@dataclass(frozen=True)
class RecommendationIntent:
    question_version: str = QUESTION_VERSION
    taxonomy_version: str = MOVIE_TAXONOMY_VERSION
    hard_constraints: dict[str, object] = field(default_factory=dict)
    required_features: frozenset[str] = frozenset()
    preferred_features: dict[str, float] = field(default_factory=dict)
    forbidden_features: frozenset[str] = frozenset()
    mood_targets: dict[str, float] = field(default_factory=dict)
    mood_importance: dict[str, float] = field(default_factory=dict)
    exploration_level: float = 0.5
    watch_context: str = "solo"

    def validate(self) -> None:
        overlap = self.required_features & self.forbidden_features
        if overlap:
            raise ValueError(f"Признак одновременно обязателен и недопустим: {sorted(overlap)}")
        for name, value in self.mood_targets.items():
            if name not in mood.MOOD_DIMENSIONS:
                raise ValueError(f"Неизвестное измерение настроения: {name}")
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"Цель настроения вне диапазона: {name}={value}")
        for feature in self.required_features | self.forbidden_features | set(self.preferred_features):
            if feature not in KNOWN_FEATURES:
                raise ValueError(f"Признак вне канонического словаря: {feature}")


KNOWN_FEATURES: frozenset[str] = frozenset(
    {str(item) for item in CanonicalGenre}
    | {str(item) for item in CanonicalTheme}
    | {str(item) for item in CanonicalTone}
    | {str(item) for item in AudienceFlag})


def _tone(name) -> str:
    return str(name)


# Канонические требования по ответам. Здесь только то, что метаданные реально
# умеют подтвердить: обещать признак, которого в профиле не бывает, — тот же
# декоративный ответ, от которого уходили в прошлом проходе.
INTENT_MAPPINGS: dict[tuple[str, str], IntentContribution] = {
    # q1 — общее направление
    ("q1", "relax"):   IntentContribution(
        preferred_features={_tone(CanonicalTone.COZY): 0.6, _tone(CanonicalTone.FEEL_GOOD): 0.4},
        forbidden_features=frozenset({_tone(CanonicalTone.DISTURBING)}), exploration=0.3),
    ("q1", "tension"): IntentContribution(
        preferred_features={_tone(CanonicalTone.SUSPENSEFUL): 0.7},
        forbidden_features=frozenset({_tone(CanonicalTone.COZY)}), exploration=0.5),
    ("q1", "humor"):   IntentContribution(
        required_features=frozenset({str(CanonicalGenre.COMEDY)}), exploration=0.4),
    ("q1", "emotion"): IntentContribution(
        preferred_features={str(CanonicalGenre.DRAMA): 0.6}, exploration=0.4),
    ("q1", "unusual"): IntentContribution(
        preferred_features={_tone(CanonicalTone.THOUGHT_PROVOKING): 0.6}, exploration=0.8),
    # relax
    ("r1", "warm"):      IntentContribution(preferred_features={_tone(CanonicalTone.COZY): 0.8}),
    ("r1", "adventure"): IntentContribution(preferred_features={str(CanonicalGenre.ADVENTURE): 0.7}),
    ("r1", "visual"):    IntentContribution(preferred_features={_tone(CanonicalTone.SLOW_BURN): 0.5}),
    ("r1", "simple"):    IntentContribution(preferred_features={str(CanonicalGenre.COMEDY): 0.7}),
    ("r2", "fast"):   IntentContribution(preferred_features={str(CanonicalGenre.ACTION): 0.4}),
    ("r2", "medium"): IntentContribution(preferred_features={str(CanonicalGenre.ADVENTURE): 0.2}),
    ("r2", "slow"):   IntentContribution(preferred_features={_tone(CanonicalTone.SLOW_BURN): 0.7}),
    ("r3", "comfort"):  IntentContribution(
        forbidden_features=frozenset({_tone(CanonicalTone.DISTURBING), _tone(CanonicalTone.BLEAK)})),
    ("r3", "positive"): IntentContribution(preferred_features={_tone(CanonicalTone.FEEL_GOOD): 0.6}),
    ("r3", "drama"):    IntentContribution(preferred_features={str(CanonicalGenre.DRAMA): 0.5}),
    # tension
    ("t1", "survival"):      IntentContribution(
        required_features=frozenset({str(CanonicalTheme.SURVIVAL)})),
    ("t1", "psychological"): IntentContribution(
        required_features=frozenset({_tone(CanonicalTone.PSYCHOLOGICAL)})),
    ("t1", "mystery"):       IntentContribution(
        required_features=frozenset({str(CanonicalGenre.MYSTERY)})),
    ("t1", "horror"):        IntentContribution(
        required_features=frozenset({str(CanonicalGenre.HORROR)})),
    ("t2_psychological", "hero"):     IntentContribution(preferred_features={_tone(CanonicalTone.PSYCHOLOGICAL): 0.8}),
    ("t2_psychological", "close"):    IntentContribution(preferred_features={str(CanonicalTheme.FAMILY_RELATIONSHIP): 0.5}),
    ("t2_psychological", "everyone"): IntentContribution(preferred_features={_tone(CanonicalTone.SUSPENSEFUL): 0.7}),
    ("t2_psychological", "eyes"):     IntentContribution(preferred_features={_tone(CanonicalTone.PSYCHOLOGICAL): 0.9}),
    ("t2_mystery", "crime"):       IntentContribution(preferred_features={str(CanonicalGenre.CRIME): 0.8}),
    ("t2_mystery", "missing"):     IntentContribution(preferred_features={str(CanonicalTheme.INVESTIGATION): 0.7}),
    ("t2_mystery", "past"):        IntentContribution(preferred_features={str(CanonicalTheme.GRIEF): 0.4}),
    ("t2_mystery", "unexplained"): IntentContribution(preferred_features={str(CanonicalTheme.SUPERNATURAL): 0.8}),
    ("t2_survival", "people"): IntentContribution(preferred_features={str(CanonicalGenre.THRILLER): 0.6}),
    ("t2_survival", "nature"): IntentContribution(preferred_features={str(CanonicalTheme.SURVIVAL): 0.8}),
    ("t2_survival", "system"): IntentContribution(preferred_features={str(CanonicalTheme.DYSTOPIA): 0.8}),
    ("t2_survival", "self"):   IntentContribution(preferred_features={_tone(CanonicalTone.PSYCHOLOGICAL): 0.7}),
    ("t2_horror", "unknown"): IntentContribution(preferred_features={str(CanonicalTheme.SUPERNATURAL): 0.7}),
    ("t2_horror", "mind"):    IntentContribution(preferred_features={_tone(CanonicalTone.PSYCHOLOGICAL): 0.8}),
    ("t2_horror", "threat"):  IntentContribution(preferred_features={str(CanonicalTheme.SURVIVAL): 0.6}),
    ("t4", "clear"): IntentContribution(preferred_features={str(CanonicalGenre.THRILLER): 0.2}),
    ("t4", "semi"):  IntentContribution(preferred_features={_tone(CanonicalTone.SUSPENSEFUL): 0.3}),
    ("t4", "open"):  IntentContribution(preferred_features={_tone(CanonicalTone.THOUGHT_PROVOKING): 0.7}),
    # humor
    ("h1", "situational"): IntentContribution(required_features=frozenset({str(CanonicalGenre.COMEDY)})),
    ("h1", "sarcastic"):   IntentContribution(preferred_features={_tone(CanonicalTone.SATIRICAL): 0.7}),
    ("h1", "absurd"):      IntentContribution(preferred_features={_tone(CanonicalTone.ABSURDIST): 0.8}),
    ("h1", "dark"):        IntentContribution(
        required_features=frozenset({_tone(CanonicalTone.DARK_COMEDY)}),
        forbidden_features=frozenset({str(AudienceFlag.CHILD_ORIENTED)})),
    ("h1", "warm"):        IntentContribution(
        preferred_features={_tone(CanonicalTone.FEEL_GOOD): 0.7},
        forbidden_features=frozenset({_tone(CanonicalTone.BLEAK)})),
    ("h2", "none"):   IntentContribution(preferred_features={_tone(CanonicalTone.LIGHTHEARTED): 0.6}),
    ("h2", "soul"):   IntentContribution(preferred_features={str(CanonicalGenre.DRAMA): 0.4}),
    ("h2", "satire"): IntentContribution(preferred_features={_tone(CanonicalTone.SATIRICAL): 0.8}),
    ("h3", "calm"):   IntentContribution(preferred_features={_tone(CanonicalTone.SLOW_BURN): 0.5}),
    ("h3", "steady"): IntentContribution(preferred_features={str(CanonicalGenre.COMEDY): 0.2}),
    ("h3", "wild"):   IntentContribution(preferred_features={_tone(CanonicalTone.ABSURDIST): 0.6}),
    # emotion
    ("e1", "romance"):    IntentContribution(preferred_features={str(CanonicalGenre.ROMANCE): 0.8}),
    ("e1", "family"):     IntentContribution(preferred_features={str(CanonicalTheme.FAMILY_RELATIONSHIP): 0.8}),
    ("e1", "friendship"): IntentContribution(preferred_features={str(CanonicalTheme.FRIENDSHIP): 0.8}),
    ("e1", "dream"):      IntentContribution(preferred_features={_tone(CanonicalTone.INSPIRING): 0.8}),
    ("e1", "loss"):       IntentContribution(preferred_features={str(CanonicalTheme.GRIEF): 0.8}),
    ("e2", "positive"):    IntentContribution(
        preferred_features={_tone(CanonicalTone.FEEL_GOOD): 0.7},
        forbidden_features=frozenset({_tone(CanonicalTone.BLEAK)})),
    ("e2", "bittersweet"): IntentContribution(preferred_features={_tone(CanonicalTone.MELANCHOLIC): 0.7}),
    ("e2", "tragic"):      IntentContribution(preferred_features={_tone(CanonicalTone.BLEAK): 0.6}),
    ("e3", "characters"): IntentContribution(preferred_features={str(CanonicalGenre.DRAMA): 0.5}),
    ("e3", "plot"):       IntentContribution(preferred_features={str(CanonicalGenre.THRILLER): 0.3}),
    ("e3", "balance"):    IntentContribution(exploration=0.5),
    # unusual
    ("u1", "visual"):     IntentContribution(preferred_features={str(CanonicalGenre.FANTASY): 0.5}),
    ("u1", "idea"):       IntentContribution(preferred_features={_tone(CanonicalTone.THOUGHT_PROVOKING): 0.8}),
    ("u1", "telling"):    IntentContribution(preferred_features={_tone(CanonicalTone.THOUGHT_PROVOKING): 0.6}),
    ("u1", "characters"): IntentContribution(preferred_features={_tone(CanonicalTone.ABSURDIST): 0.6}),
    ("u1", "reality"):    IntentContribution(preferred_features={_tone(CanonicalTone.PSYCHOLOGICAL): 0.6}),
    ("u2", "low"):    IntentContribution(exploration=0.35),
    ("u2", "medium"): IntentContribution(exploration=0.6),
    ("u2", "high"):   IntentContribution(exploration=0.9),
    ("u3", "dream"):     IntentContribution(preferred_features={str(CanonicalGenre.FANTASY): 0.7}),
    ("u3", "nightmare"): IntentContribution(preferred_features={_tone(CanonicalTone.DISTURBING): 0.7}),
    ("u3", "puzzle"):    IntentContribution(preferred_features={_tone(CanonicalTone.THOUGHT_PROVOKING): 0.9}),
    ("u3", "adventure"): IntentContribution(preferred_features={str(CanonicalGenre.ADVENTURE): 0.7}),
    # общие
    ("c1", "short"):     IntentContribution(hard_constraints={"runtime_max": 100}),
    ("c1", "two_hours"): IntentContribution(hard_constraints={"runtime_max": 130}),
    ("c1", "long"):      IntentContribution(hard_constraints={"runtime_min": 130}),
    ("c1", "any"):       IntentContribution(exploration=0.5),
    ("c2", "modern"):  IntentContribution(hard_constraints={"year_min": 2018}),
    ("c2", "classic"): IntentContribution(hard_constraints={"year_max": 2005}),
    ("c2", "any"):     IntentContribution(exploration=0.5),
    ("c3", "safe"):       IntentContribution(exploration=0.2),
    ("c3", "less_known"): IntentContribution(exploration=0.6),
    ("c3", "surprise"):   IntentContribution(exploration=0.95),
    ("c4", "solo"):  IntentContribution(watch_context="solo"),
    ("c4", "pair"):  IntentContribution(watch_context="pair"),
    ("c4", "group"): IntentContribution(watch_context="group",
                                        preferred_features={str(AudienceFlag.FAMILY_FRIENDLY): 0.3}),
}


def build_intent(answers: dict) -> RecommendationIntent:
    """Свести ответы в один канонический запрос.

    Настроение переиспользуется из mood.py, а не считается заново: это уже
    выверенная на реальном каталоге шкала, и второй её источник гарантированно
    разъехался бы с первым.
    """
    required: set[str] = set()
    forbidden: set[str] = set()
    preferred: dict[str, float] = {}
    hard: dict[str, object] = {}
    exploration: list[float] = []
    context = "solo"

    for question_id, answer in (answers or {}).items():
        values = answer if isinstance(answer, list) else [answer]
        for answer_id in values:
            contribution = INTENT_MAPPINGS.get((str(question_id), str(answer_id)))
            if contribution is None:
                continue      # неизвестные ответы игнорируем, а не угадываем
            required |= contribution.required_features
            forbidden |= contribution.forbidden_features
            for feature, weight in contribution.preferred_features.items():
                preferred[feature] = max(preferred.get(feature, 0.0), float(weight))
            hard.update(contribution.hard_constraints)
            if contribution.exploration is not None:
                exploration.append(contribution.exploration)
            if contribution.watch_context:
                context = contribution.watch_context

    # Прямой запрет сильнее пожелания: если ответ назвал признак недопустимым,
    # он не может остаться в желательных из-за соседнего ответа.
    preferred = {name: weight for name, weight in preferred.items() if name not in forbidden}
    required -= forbidden

    mood_intent = mood.build_mood_intent(answers)
    intent = RecommendationIntent(
        hard_constraints=hard,
        required_features=frozenset(required),
        preferred_features=preferred,
        forbidden_features=frozenset(forbidden),
        mood_targets=dict(mood_intent.targets),
        mood_importance=dict(mood_intent.importance),
        exploration_level=sum(exploration) / len(exploration) if exploration else 0.5,
        watch_context=context,
    )
    intent.validate()
    return intent
