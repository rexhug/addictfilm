"""Канонический словарь продукта: жанр, тема, тон, аудитория, тип записи.

Один словарь на всё. До этого модуля одни и те же понятия жили в трёх местах
разными строками: анкета спрашивала `dark`, признаки фильма считали `dark_humor`,
а объяснение показывало «чёрный юмор» из четвёртого справочника. Любое
расхождение между ними — это молчаливая потеря смысла, поэтому идентификаторы
здесь одни и переиспользуются везде.

Границы понятий держим строго раздельно:
* ЖАНР — к какой категории фильм относится;
* ТЕМА — о чём история;
* ТОН — как она ощущается;
* АУДИТОРИЯ — для кого и что внутри;
* НАСТРОЕНИЕ — числовое представление впечатления (8 измерений, см. mood.py).

Жанр «комедия» — не тон «чёрная комедия», и подменять одно другим нельзя:
ровно из этой подмены выросли рекомендации «К-9: Собачья работа» на запрос
чёрного юмора.
"""
from __future__ import annotations

from enum import StrEnum

# Смена любого из значений делает существующие профили устаревшими и запускает
# пересчёт (см. enrichment/reconciliation.py). Идентификаторы не переименовываем
# молча: старое имя в базе должно остаться читаемым.
MOVIE_TAXONOMY_VERSION = "movie-taxonomy-v1"
RULE_EXTRACTOR_VERSION = "rules-v1"
MOVIE_FEATURE_VERSION = "movie-mood-v2"


class ContentType(StrEnum):
    MOVIE = "movie"
    TV = "tv"
    EPISODE = "episode"
    SHORT = "short"
    UNKNOWN = "unknown"


# Совпадает с продуктовой витриной жанров каталога: расширять список ради
# полноты «как у провайдера» не нужно — каждый жанр должен что-то решать.
class CanonicalGenre(StrEnum):
    ACTION = "action"
    ADVENTURE = "adventure"
    ANIMATION = "animation"
    BIOGRAPHY = "biography"
    COMEDY = "comedy"
    CRIME = "crime"
    DOCUMENTARY = "documentary"
    DRAMA = "drama"
    FAMILY = "family"
    FANTASY = "fantasy"
    HISTORY = "history"
    HORROR = "horror"
    MUSIC = "music"
    MYSTERY = "mystery"
    ROMANCE = "romance"
    SCIENCE_FICTION = "science_fiction"
    THRILLER = "thriller"
    WAR = "war"


class CanonicalTheme(StrEnum):
    FRIENDSHIP = "friendship"
    FAMILY_RELATIONSHIP = "family_relationship"
    REVENGE = "revenge"
    INVESTIGATION = "investigation"
    SURVIVAL = "survival"
    COMING_OF_AGE = "coming_of_age"
    HEIST = "heist"
    POLITICAL_CONFLICT = "political_conflict"
    TIME_TRAVEL = "time_travel"
    DYSTOPIA = "dystopia"
    GRIEF = "grief"
    REDEMPTION = "redemption"
    SUPERNATURAL = "supernatural"


class CanonicalTone(StrEnum):
    COZY = "cozy"
    FEEL_GOOD = "feel_good"
    LIGHTHEARTED = "lighthearted"
    DARK_COMEDY = "dark_comedy"
    SATIRICAL = "satirical"
    ABSURDIST = "absurdist"
    BLEAK = "bleak"
    DISTURBING = "disturbing"
    INSPIRING = "inspiring"
    MELANCHOLIC = "melancholic"
    THOUGHT_PROVOKING = "thought_provoking"
    PSYCHOLOGICAL = "psychological"
    SLOW_BURN = "slow_burn"
    SUSPENSEFUL = "suspenseful"


class AudienceFlag(StrEnum):
    FAMILY_FRIENDLY = "family_friendly"
    CHILD_ORIENTED = "child_oriented"
    ADULT_ANIMATION = "adult_animation"
    MATURE_THEMES = "mature_themes"


# Источник доказательства. Порядок важен: чем ниже в списке, тем слабее голос
# при разрешении противоречий (см. enrichment/merge.py).
class EvidenceSource(StrEnum):
    PROVIDER_CONTENT_TYPE = "provider_content_type"
    MANUAL_OVERRIDE = "manual_override"
    REVIEWED_KEYWORD = "reviewed_keyword"
    CERTIFICATION = "certification"
    DETERMINISTIC_RULE = "deterministic_rule"
    SEMANTIC = "semantic"
    GENRE_BASELINE = "genre_baseline"


EVIDENCE_PRECEDENCE: tuple[str, ...] = (
    EvidenceSource.PROVIDER_CONTENT_TYPE,
    EvidenceSource.MANUAL_OVERRIDE,
    EvidenceSource.REVIEWED_KEYWORD,
    EvidenceSource.CERTIFICATION,
    EvidenceSource.DETERMINISTIC_RULE,
    EvidenceSource.SEMANTIC,
    EvidenceSource.GENRE_BASELINE,
)


def precedence_rank(source: str) -> int:
    """Меньше — сильнее. Неизвестный источник считается самым слабым."""
    try:
        return EVIDENCE_PRECEDENCE.index(source)
    except ValueError:
        return len(EVIDENCE_PRECEDENCE)


GENRES = frozenset(CanonicalGenre)
THEMES = frozenset(CanonicalTheme)
TONES = frozenset(CanonicalTone)
AUDIENCE_FLAGS = frozenset(AudienceFlag)
CONTENT_TYPES = frozenset(ContentType)

# Типы записей, недопустимые в подборе ФИЛЬМОВ.
NON_MOVIE_CONTENT = frozenset({ContentType.TV, ContentType.EPISODE, ContentType.SHORT})


def is_known(kind: str, value: str) -> bool:
    return value in {"genre": GENRES, "theme": THEMES, "tone": TONES,
                     "audience": AUDIENCE_FLAGS, "content_type": CONTENT_TYPES}.get(kind, frozenset())
