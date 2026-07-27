"""Слияние доказательств, ручные правки и расчёт уверенности.

Правило старшинства простое и одно на всю систему: чем ближе источник к
проверенному факту, тем сильнее его голос. Семантика уточняет то, чего не знает
детерминированный слой, но не может переспорить ни тип записи от провайдера, ни
ручную правку, ни сильное противоречие.
"""
from __future__ import annotations

from .extraction import TONE_MOOD_DELTAS, clamp01
from .models import ExtractedMovieFeatures, FeatureEvidence
from .taxonomy import AudienceFlag, CanonicalGenre, CanonicalTone, ContentType, EvidenceSource

# Тон, который не может сочетаться с флагом аудитории. Не «странно», а прямо
# противоречит: детская анимация не бывает взрослой чёрной комедией.
TONE_AUDIENCE_CONTRADICTIONS: dict[str, tuple[str, ...]] = {
    CanonicalTone.DARK_COMEDY: (AudienceFlag.CHILD_ORIENTED,),
    CanonicalTone.DISTURBING: (AudienceFlag.CHILD_ORIENTED, AudienceFlag.FAMILY_FRIENDLY),
    CanonicalTone.BLEAK: (AudienceFlag.CHILD_ORIENTED,),
    CanonicalTone.COZY: (),
}

# Тон требует минимума по измерению — иначе он не подтверждён, а заявлен.
TONE_MOOD_REQUIREMENTS: dict[str, dict[str, float]] = {
    CanonicalTone.DARK_COMEDY: {"humor_min": 0.55, "peak_darkness_min": 0.50},
    CanonicalTone.COZY: {"tension_max": 0.55, "darkness_max": 0.55},
    CanonicalTone.THOUGHT_PROVOKING: {"complexity_min": 0.55},
    CanonicalTone.PSYCHOLOGICAL: {"complexity_min": 0.45},
    CanonicalTone.SUSPENSEFUL: {"tension_min": 0.50},
    CanonicalTone.BLEAK: {"darkness_min": 0.50},
}

OVERRIDE_ALLOWED_KEYS = frozenset({
    "content_type", "genres", "themes", "tones", "audience_flags", "mood", "status",
})


def _satisfies(mood_values: dict[str, float], requirements: dict[str, float]) -> bool:
    for key, limit in requirements.items():
        dimension, _, bound = key.rpartition("_")
        value = float(mood_values.get(dimension, 0.5))
        if bound == "min" and value < limit:
            return False
        if bound == "max" and value > limit:
            return False
    return True


class MovieFeatureMerger:
    """Единственное место, где доказательства превращаются в профиль."""

    def merge(self, *, deterministic: ExtractedMovieFeatures,
              semantic: tuple[FeatureEvidence, ...] = (),
              overrides: dict | None = None) -> tuple[ExtractedMovieFeatures, int]:
        """(итоговый профиль, число противоречий)."""
        mood_values = dict(deterministic.mood)
        tones = set(deterministic.tones)
        contradictions = 0

        # 1. Семантика добавляет только то, чего ещё нет, и только если не спорит
        # с уже посчитанными измерениями.
        accepted_semantic: list[FeatureEvidence] = []
        for item in semantic:
            label = item.canonical_id
            if label in tones:
                continue      # проверенный маркер уже сказал это сильнее
            requirements = TONE_MOOD_REQUIREMENTS.get(label, {})
            if requirements and not _satisfies(mood_values, requirements):
                contradictions += 1
                continue
            tones.add(label)
            accepted_semantic.append(item)
            # Семантический тон двигает измерения слабее проверенного маркера.
            for dimension, delta in TONE_MOOD_DELTAS.get(label, {}).items():
                mood_values[dimension] = clamp01(mood_values[dimension] + delta * 0.5)

        mood_values["peak_darkness"] = clamp01(
            max(mood_values.get("peak_darkness", 0.0), mood_values["darkness"]))

        # 2. Тон, не подтверждённый измерениями, — это противоречие, а не признак.
        confirmed_tones = set()
        for tone in tones:
            requirements = TONE_MOOD_REQUIREMENTS.get(tone, {})
            if requirements and not _satisfies(mood_values, requirements):
                contradictions += 1
                continue
            confirmed_tones.add(tone)

        # 3. Флаг аудитории против измерений. Возрастной рейтинг — грубая шкала,
        # и против выверенной мрачности он проигрывает: профиль, где стоит
        # «семейное» при мрачности 0.8, противоречит сам себе. Снимаем ярлык, а
        # не выбрасываем весь профиль — числа-то посчитаны верно.
        audience = set(deterministic.audience_flags)
        if mood_values["darkness"] > 0.70:
            for flag in (AudienceFlag.FAMILY_FRIENDLY, AudienceFlag.CHILD_ORIENTED):
                if str(flag) in audience:
                    audience.discard(str(flag))
                    contradictions += 1
        # Флаги аудитории против тона.
        for tone in list(confirmed_tones):
            forbidden = set(TONE_AUDIENCE_CONTRADICTIONS.get(tone, ()))
            if forbidden & audience:
                contradictions += 1
                confirmed_tones.discard(tone)

        merged = ExtractedMovieFeatures(
            content_type=deterministic.content_type,
            genres=deterministic.genres,
            themes=deterministic.themes,
            tones=frozenset(confirmed_tones),
            audience_flags=frozenset(audience),
            mood={dim: round(value, 4) for dim, value in mood_values.items()},
            confidence=deterministic.confidence,
            evidence=deterministic.evidence + tuple(accepted_semantic),
        )
        # 4. Ручная правка применяется последней и переспорить её нечем.
        return (self._apply_overrides(merged, overrides), contradictions)

    @staticmethod
    def _apply_overrides(features: ExtractedMovieFeatures,
                         overrides: dict | None) -> ExtractedMovieFeatures:
        if not overrides:
            return features
        data = {key: value for key, value in overrides.items() if key in OVERRIDE_ALLOWED_KEYS}
        if not data:
            return features
        mood_values = dict(features.mood)
        for dimension, value in (data.get("mood") or {}).items():
            if dimension in mood_values:
                mood_values[dimension] = clamp01(float(value))
        mood_values["peak_darkness"] = clamp01(
            max(mood_values.get("peak_darkness", 0.0), mood_values["darkness"]))
        content_type = data.get("content_type")
        evidence = features.evidence + tuple(
            FeatureEvidence(EvidenceSource.MANUAL_OVERRIDE, str(key), 1.0, 1.0)
            for key in sorted(data))
        return ExtractedMovieFeatures(
            content_type=ContentType(content_type) if content_type else features.content_type,
            genres=frozenset(data.get("genres", features.genres)),
            themes=frozenset(data.get("themes", features.themes)),
            tones=frozenset(data.get("tones", features.tones)),
            audience_flags=frozenset(data.get("audience_flags", features.audience_flags)),
            mood=mood_values,
            confidence=features.confidence,
            evidence=evidence,
        )


def calculate_profile_confidence(*, has_valid_content_type: bool, genre_count: int,
                                 reviewed_marker_count: int, has_overview: bool,
                                 semantic_confidence: float, agreement_score: float,
                                 contradiction_count: int) -> float:
    """Уверенность — про КАЧЕСТВО доказательств, а не про их количество.

    Веса откалиброваны на реальном каталоге: 288 фильмов, у большинства есть
    жанры и описание, но почти нет проверенных маркеров тона.
    """
    score = 0.0
    if has_valid_content_type:
        score += 0.22
    if genre_count:
        score += 0.20 if genre_count >= 2 else 0.14
    score += min(reviewed_marker_count, 5) * 0.06
    if has_overview:
        score += 0.14
    score += 0.12 * clamp01(semantic_confidence)
    score += 0.08 * clamp01(agreement_score)
    score -= min(contradiction_count, 3) * 0.10
    return clamp01(score)


PROFILE_STATUS_THRESHOLDS = {"ready": 0.55, "low_confidence": 0.35}


def resolve_status(confidence: float, *, invalid: bool) -> str:
    from .models import PROFILE_FAILED, PROFILE_LOW_CONFIDENCE, PROFILE_READY
    if invalid:
        return PROFILE_FAILED
    if confidence >= PROFILE_STATUS_THRESHOLDS["ready"]:
        return PROFILE_READY
    if confidence >= PROFILE_STATUS_THRESHOLDS["low_confidence"]:
        return PROFILE_LOW_CONFIDENCE
    return PROFILE_LOW_CONFIDENCE


def agreement(deterministic: ExtractedMovieFeatures,
              semantic: tuple[FeatureEvidence, ...]) -> float:
    """Насколько семантика согласна с проверенными маркерами."""
    if not semantic:
        return 0.5      # молчание — не согласие и не спор
    labels = {item.canonical_id for item in semantic}
    if not deterministic.tones:
        return 0.5
    overlap = len(labels & deterministic.tones)
    return clamp01(overlap / max(1, len(labels)))


# Жанр без единого канонического соответствия — повод не доверять профилю.
def has_usable_genres(features: ExtractedMovieFeatures) -> bool:
    return bool(features.genres - {str(CanonicalGenre.ANIMATION)})
