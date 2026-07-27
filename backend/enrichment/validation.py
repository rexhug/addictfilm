"""Автоматические проверки здравого смысла над готовым профилем.

Смысл не в том, чтобы поймать «некрасивые» числа, а в том, чтобы профиль,
который сам себе противоречит, не попал в строгий подбор. Комедия без юмора и
ужасы без напряжения — это не редкий случай, а сломанное извлечение.

Уровни:
* valid          — годен;
* warning        — странно, но пользоваться можно;
* low_confidence — годен только для широких запросов;
* invalid        — в строгие рекомендации не допускается.
"""
from __future__ import annotations

from .models import ExtractedMovieFeatures, ValidationOutcome
from .taxonomy import AudienceFlag, CanonicalGenre, CanonicalTone, ContentType

# (условие, уровень, текст). Условие — функция от профиля.
_CHECKS: tuple[tuple[str, str, object], ...] = (
    ("comedy_without_humor", "warning",
     lambda f: str(CanonicalGenre.COMEDY) in f.genres and f.mood.get("humor", 1) < 0.30),
    ("horror_without_tension", "warning",
     lambda f: str(CanonicalGenre.HORROR) in f.genres and f.mood.get("tension", 1) < 0.50),
    ("documentary_without_realism", "warning",
     lambda f: str(CanonicalGenre.DOCUMENTARY) in f.genres and f.mood.get("realism", 1) < 0.70),
    ("family_friendly_but_dark", "invalid",
     lambda f: str(AudienceFlag.FAMILY_FRIENDLY) in f.audience_flags
     and f.mood.get("darkness", 0) > 0.75),
    ("cozy_but_tense", "invalid",
     lambda f: str(CanonicalTone.COZY) in f.tones and f.mood.get("tension", 0) > 0.60),
    ("dark_comedy_without_humor", "invalid",
     lambda f: str(CanonicalTone.DARK_COMEDY) in f.tones and f.mood.get("humor", 0) < 0.50),
    ("dark_comedy_without_darkness", "invalid",
     lambda f: str(CanonicalTone.DARK_COMEDY) in f.tones
     and f.mood.get("peak_darkness", 0) < 0.40),
    ("thought_provoking_without_complexity", "low_confidence",
     lambda f: str(CanonicalTone.THOUGHT_PROVOKING) in f.tones
     and f.mood.get("complexity", 0) < 0.55),
    ("child_and_adult_animation", "invalid",
     lambda f: {str(AudienceFlag.CHILD_ORIENTED), str(AudienceFlag.ADULT_ANIMATION)}
     <= set(f.audience_flags)),
    ("unknown_content_type", "low_confidence",
     lambda f: f.content_type is ContentType.UNKNOWN),
)

_SEVERITY = {"valid": 0, "warning": 1, "low_confidence": 2, "invalid": 3}


def validate(features: ExtractedMovieFeatures) -> ValidationOutcome:
    warnings: list[str] = []
    level = "valid"
    for name, severity, predicate in _CHECKS:
        try:
            triggered = bool(predicate(features))
        except Exception:
            continue
        if triggered:
            warnings.append(name)
            if _SEVERITY[severity] > _SEVERITY[level]:
                level = severity
    for dimension, value in features.mood.items():
        if not 0.0 <= float(value) <= 1.0:
            warnings.append(f"out_of_range:{dimension}")
            level = "invalid"
    return ValidationOutcome(level=level, warnings=tuple(warnings))
