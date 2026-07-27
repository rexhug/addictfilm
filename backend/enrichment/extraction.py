"""Детерминированное извлечение признаков: базовый слой, который есть всегда.

Семантический классификатор — необязательное усиление. Если он выключен, не
установлен или упал, продукт обязан продолжать работать на этом слое, поэтому
здесь нет ни сети, ни моделей, ни случайности: одинаковый вход всегда даёт
одинаковый выход.

Числовые профили настроения по жанрам переиспользуются из mood.py — это уже
выверенные на реальном каталоге гипотезы, и заводить им второй источник правды
означало бы гарантированное расхождение.
"""
from __future__ import annotations

import mood

from . import mappings
from .models import ExtractedMovieFeatures, FeatureEvidence, NormalizedMovieSource
from .taxonomy import AudienceFlag, CanonicalGenre, CanonicalTone, EvidenceSource

MOOD_DIMENSIONS = mood.MOOD_DIMENSIONS

# Канонический жанр → жанр каталога, под которым в mood.py лежит числовой профиль.
_CANONICAL_TO_MOOD_GENRE = {
    CanonicalGenre.ACTION: "боевик",
    CanonicalGenre.ADVENTURE: "приключения",
    CanonicalGenre.ANIMATION: "мультфильм",
    CanonicalGenre.BIOGRAPHY: "биография",
    CanonicalGenre.COMEDY: "комедия",
    CanonicalGenre.CRIME: "криминал",
    CanonicalGenre.DOCUMENTARY: "документальный",
    CanonicalGenre.DRAMA: "драма",
    CanonicalGenre.FAMILY: "семейный",
    CanonicalGenre.FANTASY: "фэнтези",
    CanonicalGenre.HISTORY: "история",
    CanonicalGenre.HORROR: "ужасы",
    CanonicalGenre.MUSIC: "мюзикл",
    CanonicalGenre.MYSTERY: "детектив",
    CanonicalGenre.ROMANCE: "мелодрама",
    CanonicalGenre.SCIENCE_FICTION: "фантастика",
    CanonicalGenre.THRILLER: "триллер",
    CanonicalGenre.WAR: "военный",
}

# Тон → сдвиг измерений. Тон описывает ОЩУЩЕНИЕ, поэтому он двигает настроение,
# а не наоборот. Суммарный сдвиг ограничен, чтобы длинное описание с десятком
# совпадений не уводило измерение в край.
TONE_MOOD_DELTAS: dict[str, dict[str, float]] = {
    CanonicalTone.DARK_COMEDY:       {"humor": .22, "darkness": .28, "tension": .08},
    CanonicalTone.SATIRICAL:         {"humor": .18, "complexity": .16},
    CanonicalTone.ABSURDIST:         {"humor": .16, "realism": -.22},
    CanonicalTone.PSYCHOLOGICAL:     {"complexity": .26, "tension": .20, "darkness": .16},
    CanonicalTone.SLOW_BURN:         {"pace": -.30, "complexity": .14, "tension": .10},
    CanonicalTone.COZY:              {"darkness": -.30, "tension": -.22, "emotionality": .16},
    CanonicalTone.FEEL_GOOD:         {"darkness": -.24, "emotionality": .16},
    CanonicalTone.LIGHTHEARTED:      {"darkness": -.18, "tension": -.16},
    CanonicalTone.BLEAK:             {"darkness": .32, "emotionality": .18, "humor": -.16},
    CanonicalTone.DISTURBING:        {"darkness": .30, "tension": .22},
    CanonicalTone.INSPIRING:         {"emotionality": .24, "darkness": -.12},
    CanonicalTone.MELANCHOLIC:       {"emotionality": .26, "darkness": .14, "pace": -.10},
    CanonicalTone.THOUGHT_PROVOKING: {"complexity": .30, "pace": -.08},
    CanonicalTone.SUSPENSEFUL:       {"tension": .24, "pace": .08},
}

THEME_MOOD_DELTAS: dict[str, dict[str, float]] = {
    "revenge":             {"darkness": .22, "tension": .18},
    "survival":            {"tension": .26, "energy": .14, "darkness": .12},
    "investigation":       {"complexity": .22, "tension": .16, "realism": .08},
    "coming_of_age":       {"emotionality": .26, "darkness": -.08},
    "grief":               {"darkness": .28, "emotionality": .30, "humor": -.14},
    "dystopia":            {"darkness": .28, "complexity": .14, "realism": -.12},
    "time_travel":         {"complexity": .18, "realism": -.22},
    "supernatural":        {"realism": -.30, "tension": .14},
    "family_relationship": {"emotionality": .18, "darkness": -.12},
    "friendship":          {"emotionality": .18, "darkness": -.10},
    "heist":               {"tension": .18, "pace": .12},
    "political_conflict":  {"complexity": .18, "realism": .12},
    "redemption":          {"emotionality": .22},
}

MAX_TOTAL_DELTA = 0.45
# Насколько измерение подтягивается к самому выраженному жанру. Чистое среднее
# топит определяющий жанр: у «мультфильм, комедия, приключения, фэнтези» юмор
# падал до 0.28, хотя комедия там заявлена.
SIGNATURE_GENRE_PULL = 0.66
PRESENCE_DIMENSIONS = ("humor", "tension", "complexity", "energy", "pace", "emotionality")


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _genre_baseline(canonical: frozenset[str]) -> tuple[dict[str, float], float]:
    """(значения измерений, самая мрачная жанровая составляющая).

    peak_darkness считаем отдельно: у любой чёрной комедии жанр «комедия» тянет
    усреднённый тон вниз, и вопрос «есть ли здесь вообще мрачное» без него
    остаётся без ответа.
    """
    profiles = [mood.GENRE_MOOD_BASE[name] for name in
                (_CANONICAL_TO_MOOD_GENRE.get(g) for g in sorted(canonical))
                if name in mood.GENRE_MOOD_BASE]
    if not profiles:
        return {dim: 0.5 for dim in MOOD_DIMENSIONS}, 0.5
    values = {dim: sum(p[dim] for p in profiles) / len(profiles) for dim in MOOD_DIMENSIONS}
    for dim in PRESENCE_DIMENSIONS:
        strongest = max(p[dim] for p in profiles)
        if strongest > values[dim]:
            values[dim] += (strongest - values[dim]) * SIGNATURE_GENRE_PULL
    return values, max(p["darkness"] for p in profiles)


def _apply_deltas(values: dict[str, float], deltas: dict[str, dict[str, float]],
                  labels, accumulated: dict[str, float]) -> dict[str, float]:
    """Бюджет сдвига ОБЩИЙ для тонов и тем.

    Раньше у каждой группы был свой, и описание с десятком совпадений уводило
    измерение на 0.90 вместо заявленных 0.45 — предел переставал быть пределом.
    """
    for label in sorted(labels):
        for dimension, delta in deltas.get(label, {}).items():
            room = MAX_TOTAL_DELTA - abs(accumulated[dimension])
            if room <= 0:
                continue
            applied = max(-room, min(room, delta))
            accumulated[dimension] += applied
            values[dimension] = clamp01(values[dimension] + applied)
    return values


def _audience_flags(source: NormalizedMovieSource, genres: frozenset[str],
                    values: dict[str, float]) -> frozenset[str]:
    flags = set(mappings.audience_from_certification(source.certification, source.age_rating))
    # Мультфильм сам по себе НЕ означает «для детей»: взрослая анимация
    # существует, и путать их — прямая дорога к неуместной рекомендации.
    # Но возрастной рейтинг — источник сильнее эвристики по мрачности: если
    # провайдер явно сказал «для детей», догадка по тону его не отменяет.
    explicit_child = str(AudienceFlag.CHILD_ORIENTED) in flags
    if str(CanonicalGenre.ANIMATION) in genres and not explicit_child:
        if str(AudienceFlag.MATURE_THEMES) in flags or values["darkness"] >= 0.62:
            flags.add(str(AudienceFlag.ADULT_ANIMATION))
            flags.discard(str(AudienceFlag.CHILD_ORIENTED))
            flags.discard(str(AudienceFlag.FAMILY_FRIENDLY))
    if str(CanonicalGenre.FAMILY) in genres and values["darkness"] <= 0.35:
        flags.add(str(AudienceFlag.FAMILY_FRIENDLY))
    return frozenset(flags)


def extract(source: NormalizedMovieSource) -> ExtractedMovieFeatures:
    genres, unknown_genres = mappings.canonical_genres(source.genre_names)
    text = source.text()
    tones = mappings.match_reviewed_tones(text)
    themes = mappings.match_reviewed_themes(text)

    values, peak_darkness = _genre_baseline(genres)
    budget = {dim: 0.0 for dim in MOOD_DIMENSIONS}
    values = _apply_deltas(values, TONE_MOOD_DELTAS, tones, budget)
    values = _apply_deltas(values, THEME_MOOD_DELTAS, themes, budget)

    # Длительность — слабый модификатор темпа, а не правило «длинное = медленное».
    if source.runtime_minutes:
        if source.runtime_minutes >= 150:
            values["pace"] = clamp01(values["pace"] - 0.06)
        elif source.runtime_minutes <= 95:
            values["pace"] = clamp01(values["pace"] + 0.05)

    values["peak_darkness"] = clamp01(max(values["darkness"], peak_darkness))
    audience = _audience_flags(source, genres, values)

    evidence: list[FeatureEvidence] = [
        FeatureEvidence(EvidenceSource.PROVIDER_CONTENT_TYPE, str(source.content_type), 1.0, 1.0),
    ]
    evidence += [FeatureEvidence(EvidenceSource.GENRE_BASELINE, genre, 1.0, 0.9)
                 for genre in sorted(genres)]
    evidence += [FeatureEvidence(EvidenceSource.REVIEWED_KEYWORD, tone, 1.0, 0.85,
                                 {"kind": "tone"}) for tone in sorted(tones)]
    evidence += [FeatureEvidence(EvidenceSource.REVIEWED_KEYWORD, theme, 1.0, 0.75,
                                 {"kind": "theme"}) for theme in sorted(themes)]
    evidence += [FeatureEvidence(EvidenceSource.CERTIFICATION, flag, 1.0, 0.8)
                 for flag in sorted(audience)]
    if unknown_genres:
        # Нераспознанное живёт только в диагностике и никогда не становится тегом.
        evidence.append(FeatureEvidence(EvidenceSource.GENRE_BASELINE, "unmapped", 0.0, 0.0,
                                        {"labels": list(unknown_genres)}))

    return ExtractedMovieFeatures(
        content_type=source.content_type,
        genres=genres, themes=themes, tones=tones, audience_flags=audience,
        mood={dim: round(value, 4) for dim, value in values.items()},
        confidence=0.0,           # считается в merge.py по качеству доказательств
        evidence=tuple(evidence),
    )
