"""Умный случайный выбор: явные стратегии вместо лотереи по топ-80.

Смысл функции для человека: «дай одно хорошее непросмотренное кино, не задавая
вопросов». Прежняя реализация брала первые 80 кандидатов по базовому счёту и
разыгрывала их с весом качества — из-за чего одни и те же крепкие фильмы
выпадали снова и снова, а всё, что не попало в топ-80, не имело шансов вообще.

Теперь сначала выбирается СТРАТЕГИЯ (что человек получит), а уже потом фильм
внутри неё. Стратегия честно называется в причине: «надёжный выбор» и «находка»
это разные обещания, и подменять одно другим нельзя.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

import reasons as reason_codes

SMART_RANDOM_POLICY_VERSION = "smart-random-v1"

RELIABLE = "reliable"
TASTE_MATCH = "taste_match"
DISCOVERY = "discovery"

STRATEGY_REASONS = {
    RELIABLE: reason_codes.RANDOM_RELIABLE,
    TASTE_MATCH: reason_codes.RANDOM_TASTE_MATCH,
    DISCOVERY: reason_codes.RANDOM_DISCOVERY,
}


@dataclass(frozen=True)
class SmartRandomPolicyConfig:
    version: str = SMART_RANDOM_POLICY_VERSION
    strategy_weights: dict[str, float] = field(
        default_factory=lambda: {RELIABLE: 0.45, TASTE_MATCH: 0.30, DISCOVERY: 0.25})
    # Пороги качества откалиброваны по реальному каталогу (медиана 7.18, p25 6.83).
    quality_floors: dict[str, float] = field(
        default_factory=lambda: {RELIABLE: 7.0, TASTE_MATCH: 6.6, DISCOVERY: 6.3})
    # Ниже этого доверия к профилю «похоже на твой вкус» — это выдумка.
    minimum_profile_confidence: float = 0.35
    # Известность: «находка» не должна быть тем же блокбастером.
    discovery_max_popularity: float = 0.62
    maximum_pool_size: int = 500
    # Порядок отката, когда выбранная корзина пуста.
    fallback_order: tuple[str, ...] = (RELIABLE, TASTE_MATCH, DISCOVERY)


DEFAULT_POLICY = SmartRandomPolicyConfig()


def resolve_strategy_weights(policy: SmartRandomPolicyConfig,
                             profile_confidence: float) -> dict[str, float]:
    """У новичка доля «под твой вкус» перераспределяется, а не тратится впустую.

    Обещать совпадение со вкусом, ничего о вкусе не зная, — прямой обман.
    """
    weights = dict(policy.strategy_weights)
    if profile_confidence >= policy.minimum_profile_confidence:
        return weights
    taste = weights.pop(TASTE_MATCH, 0.0)
    weights[RELIABLE] = weights.get(RELIABLE, 0.0) + taste * 0.6
    weights[DISCOVERY] = weights.get(DISCOVERY, 0.0) + taste * 0.4
    weights[TASTE_MATCH] = 0.0
    return weights


def choose_strategy(weights: dict[str, float], *, rng=None) -> str:
    candidates = [(name, weight) for name, weight in weights.items() if weight > 0]
    if not candidates:
        return RELIABLE
    rng = rng or secrets.SystemRandom()
    names = [name for name, _ in candidates]
    return rng.choices(names, weights=[weight for _, weight in candidates], k=1)[0]


def _popularity(movie: dict) -> float:
    """0…1. Новизна уже посчитана движком; используем её как обратную известность."""
    return max(0.0, min(1.0, 1.0 - float(movie.get("_novelty", 0.0)) / 5.0))


def build_pool(strategy: str, ranked: list[dict], policy: SmartRandomPolicyConfig,
               profile_confidence: float) -> list[dict]:
    """Кандидаты, которые ДЕЙСТВИТЕЛЬНО соответствуют обещанию стратегии."""
    floor = policy.quality_floors[strategy]
    pool = [movie for movie in ranked[:policy.maximum_pool_size]
            if float(movie.get("_quality", 0.0)) >= floor]
    if strategy == RELIABLE:
        # Фильм, к которому у человека (или у пары) выраженная неприязнь,
        # «надёжным выбором» быть не может по определению.
        return [movie for movie in pool if float(movie.get("_affinity", 0.0)) >= 0.0]
    if strategy == TASTE_MATCH:
        if profile_confidence < policy.minimum_profile_confidence:
            return []
        return [movie for movie in pool if float(movie.get("_affinity", 0.0)) > 0.0]
    return [movie for movie in pool
            if _popularity(movie) <= policy.discovery_max_popularity
            and float(movie.get("_affinity", 0.0)) >= 0.0]


def _weight_for(strategy: str, movie: dict) -> float:
    """Вес внутри корзины. Не жёсткая сортировка: иначе снова один и тот же топ."""
    quality = float(movie.get("_quality", 0.0))
    if strategy == RELIABLE:
        return max(0.05, (quality - 5.5) ** 2)
    if strategy == TASTE_MATCH:
        return max(0.05, (quality - 5.5) * (1.0 + float(movie.get("_affinity", 0.0))))
    return max(0.05, (quality - 5.5) * (1.0 + float(movie.get("_novelty", 0.0))))


def select(ranked: list[dict], *, profile_confidence: float,
           policy: SmartRandomPolicyConfig = DEFAULT_POLICY, rng=None
           ) -> tuple[dict | None, str, bool]:
    """(фильм, стратегия, был ли откат).

    Откат явный и упорядоченный: пустая корзина не должна означать пустой экран,
    но и подменять обещание молча нельзя — стратегия возвращается наружу.
    """
    if not ranked:
        return None, RELIABLE, False
    rng = rng or secrets.SystemRandom()
    weights = resolve_strategy_weights(policy, profile_confidence)
    chosen = choose_strategy(weights, rng=rng)

    attempts = [chosen] + [name for name in policy.fallback_order if name != chosen]
    for index, strategy in enumerate(attempts):
        pool = build_pool(strategy, ranked, policy, profile_confidence)
        if not pool:
            continue
        movie = rng.choices(pool, weights=[_weight_for(strategy, movie) for movie in pool], k=1)[0]
        return movie, strategy, index > 0
    # Ни одна корзина не набралась — отдаём лучшее из доступного, не выдумывая
    # обещаний: стратегия остаётся reliable, но причина будет только фактической.
    return ranked[0], RELIABLE, True


def reason_codes_for(strategy: str, movie: dict, *, pair: bool,
                     policy: SmartRandomPolicyConfig = DEFAULT_POLICY) -> list[str]:
    """Причины строго по доказательствам: стратегия сама по себе не даёт права
    написать «высокая оценка», если фильм её не набрал."""
    codes = [reason_codes.UNSEEN_PICK, STRATEGY_REASONS[strategy]]
    if pair:
        codes.append(reason_codes.PAIR_FRIENDLY)
    if float(movie.get("_quality", 0.0)) >= 7.4:
        codes.append(reason_codes.HIGH_QUALITY)
    if strategy == DISCOVERY and _popularity(movie) <= policy.discovery_max_popularity:
        codes.append(reason_codes.HIDDEN_GEM)
    if strategy == TASTE_MATCH and float(movie.get("_affinity", 0.0)) > 0:
        codes.append(reason_codes.MATCHES_USER_TASTE)
    return codes
