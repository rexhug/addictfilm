"""Доказательства признаков: четыре состояния вместо «есть / нет».

Разница между «признака нет» и «мы не знаем» — принципиальная. Показать фильм
как соответствие обязательному признаку, которого система не подтвердила, —
это выдумать подтверждение. Поэтому UNKNOWN никогда не выполняет required: не
«нет», но и не доказательство «да».

Старые пороги настроения допустимы ТОЛЬКО как явно разрешённый supported-довод
для конкретного признака, а не как универсальный запасной путь. Иначе V2
формально покажет ноль невыполненных требований, а фактически продолжит
вигадувати підтвердження.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from enrichment.taxonomy import AudienceFlag, CanonicalGenre, CanonicalTone


class EvidenceLevel(Enum):
    DIRECT = "direct"              # признак прямо есть в готовом профиле
    SUPPORTED = "supported"        # разрешённый детерминированный proxy
    CONTRADICTED = "contradicted"  # профиль прямо противоречит признаку
    UNKNOWN = "unknown"            # данных недостаточно


@dataclass(frozen=True)
class FeatureRequirementPolicy:
    feature: str
    evidence_policy: Literal["direct_only", "direct_or_supported"] = "direct_only"
    minimum_confidence: float = 0.0


# Косвенный довод допускается точечно и с обоснованием. Для чёрной комедии он
# повторяет уже выверенное составное требование: сатира ПЛЮС реальный юмор ПЛЮС
# по-настоящему мрачная составляющая.
SupportRule = Callable[[dict, dict], bool]

SUPPORTED_RULES: dict[str, SupportRule] = {
    str(CanonicalTone.DARK_COMEDY): lambda profile, mood: (
        str(CanonicalTone.SATIRICAL) in set(profile.get("tones") or ())
        and float(mood.get("humor", 0.0)) >= 0.55
        and float(mood.get("peak_darkness", 0.0)) >= 0.72
    ),
}

# Прямое противоречие: профиль не «молчит», а говорит обратное.
CONTRADICTION_RULES: dict[str, SupportRule] = {
    str(CanonicalTone.DARK_COMEDY): lambda profile, mood: (
        str(AudienceFlag.CHILD_ORIENTED) in set(profile.get("audience_flags") or ())
        or float(mood.get("humor", 1.0)) < 0.35
    ),
    str(CanonicalTone.COZY): lambda profile, mood: float(mood.get("darkness", 0.0)) > 0.70,
    str(CanonicalGenre.HORROR): lambda profile, mood: float(mood.get("tension", 1.0)) < 0.35,
}

FEATURE_POLICIES: dict[str, FeatureRequirementPolicy] = {
    str(CanonicalTone.DARK_COMEDY): FeatureRequirementPolicy(
        str(CanonicalTone.DARK_COMEDY), "direct_or_supported", minimum_confidence=0.45),
}
# По умолчанию — только прямое доказательство. Вектор настроения не может
# доказать открытый финал или исчезновение человека, поэтому «почти подходит»
# здесь не считается.
DEFAULT_POLICY = FeatureRequirementPolicy("*", "direct_only")

_READY_STATUSES = ("ready", "stale")


def policy_for(feature: str) -> FeatureRequirementPolicy:
    return FEATURE_POLICIES.get(feature, DEFAULT_POLICY)


def profile_features(profile: dict | None) -> frozenset[str]:
    if not profile:
        return frozenset()
    return frozenset(
        set(profile.get("genres") or ()) | set(profile.get("themes") or ())
        | set(profile.get("tones") or ()) | set(profile.get("audience_flags") or ()))


def evaluate(feature: str, candidate: dict) -> EvidenceLevel:
    """Уровень доказательства признака у конкретного кандидата."""
    profile = candidate.get("_profile")
    if not profile or profile.get("status") not in _READY_STATUSES:
        return EvidenceLevel.UNKNOWN
    if profile.get("validation_level") == "invalid":
        return EvidenceLevel.UNKNOWN
    mood_values = profile.get("mood") or {}

    contradiction = CONTRADICTION_RULES.get(feature)
    if contradiction and contradiction(profile, mood_values):
        return EvidenceLevel.CONTRADICTED
    if feature in profile_features(profile):
        return EvidenceLevel.DIRECT
    support = SUPPORTED_RULES.get(feature)
    if support and support(profile, mood_values):
        return EvidenceLevel.SUPPORTED
    return EvidenceLevel.UNKNOWN


def satisfies_required(feature: str, candidate: dict) -> bool:
    """DIRECT — да; SUPPORTED — только если политика это разрешает;
    CONTRADICTED и UNKNOWN — нет."""
    level = evaluate(feature, candidate)
    if level is EvidenceLevel.CONTRADICTED or level is EvidenceLevel.UNKNOWN:
        return False
    policy = policy_for(feature)
    profile = candidate.get("_profile") or {}
    if float(profile.get("confidence") or 0.0) < policy.minimum_confidence:
        return False
    if level is EvidenceLevel.DIRECT:
        return True
    return policy.evidence_policy == "direct_or_supported"


def violates_forbidden(feature: str, candidate: dict) -> bool:
    """Отсекаем только по ИЗВЕСТНОМУ подтверждению: незнание — не улика."""
    return evaluate(feature, candidate) is EvidenceLevel.DIRECT


def evidence_summary(features, candidate: dict) -> dict[str, int]:
    """Счётчики для оценки: видно, понимает ли движок каталог или просто
    отсекает всё незнакомое."""
    counts = {level.value: 0 for level in EvidenceLevel}
    for feature in features:
        counts[evaluate(feature, candidate).value] += 1
    return counts
