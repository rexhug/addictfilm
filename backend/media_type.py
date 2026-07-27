"""Тип записи каталога: полный метр, сериал, эпизод или короткометражка.

Каталог смешанный: kinopoisk отдаёт поле ``type``, OMDb — ``Type``, а часть
записей приехала из старого бота вообще без этого признака. Подбор фильмов
обязан работать только с полнометражным кино, и определять это по длительности
нельзя: 22-минутный мультсериал и 22-минутная короткометражка неразличимы по
runtime, но это разные вещи. Поэтому тип хранится отдельной колонкой
``films.media_type`` и приходит от провайдера, а не выводится эвристикой.
"""
from __future__ import annotations

MOVIE = "movie"
SERIES = "series"
EPISODE = "episode"
SHORT = "short"
UNKNOWN = "unknown"

KNOWN_TYPES = (MOVIE, SERIES, EPISODE, SHORT, UNKNOWN)
# Всё, что не полный метр, в подборе фильмов не участвует.
EXCLUDED_FROM_MOVIE_FLOW = (SERIES, EPISODE, SHORT)

# kinopoisk.dev: movie | tv-series | cartoon | anime | animated-series | tv-show.
# «anime» у них покрывает и полнометражки, и сериалы — честнее оставить unknown,
# чем угадать и выкинуть полнометражное аниме из подбора.
_KINOPOISK_TYPES = {
    "movie": MOVIE,
    "cartoon": MOVIE,
    "tv-series": SERIES,
    "animated-series": SERIES,
    "tv-show": SERIES,
    "anime": UNKNOWN,
}
_OMDB_TYPES = {"movie": MOVIE, "series": SERIES, "episode": EPISODE}

# Короткий метр провайдеры помечают не типом, а жанром: OMDb — «Short»,
# kinopoisk — «короткометражка». Это проверенный признак формата, а не догадка
# по длительности.
_SHORT_GENRES = ("короткометражка", "short")


def normalize(value: object) -> str:
    """Любое сохранённое значение → один из KNOWN_TYPES."""
    text = str(value or "").strip().casefold()
    return text if text in KNOWN_TYPES else UNKNOWN


def from_kinopoisk(doc: dict) -> str:
    resolved = _KINOPOISK_TYPES.get(str((doc or {}).get("type") or "").strip().casefold(), UNKNOWN)
    if resolved is not UNKNOWN:
        return resolved
    # Для неоднозначного «anime» у kinopoisk есть прямой булев признак — он и
    # решает, вместо догадки по длительности.
    is_series = (doc or {}).get("isSeries")
    if isinstance(is_series, bool):
        return SERIES if is_series else MOVIE
    return UNKNOWN


def from_omdb(data: dict) -> str:
    return _OMDB_TYPES.get(str((data or {}).get("Type") or "").strip().casefold(), UNKNOWN)


def _has_short_genre(film: dict) -> bool:
    genres = str((film or {}).get("genres") or "").casefold()
    return any(marker in genres for marker in _SHORT_GENRES)


def resolve(film: dict) -> str:
    """Итоговый тип записи с учётом жанрового признака короткого метра.

    Провайдеры отдают короткометражку как ``movie``, помечая формат жанром, —
    поэтому сохранённый тип здесь уточняется, а не переписывается вслепую.
    """
    stored = normalize((film or {}).get("media_type"))
    if stored in (MOVIE, UNKNOWN) and _has_short_genre(film):
        return SHORT
    return stored


def is_movie_flow_eligible(film: dict) -> bool:
    """Годится ли запись для подбора ФИЛЬМОВ.

    Неизвестный тип пропускаем: это legacy-записи без метаданных провайдера, и
    закрывать их значило бы обнулить подбор до конца бэкфила. Всё, что провайдер
    назвал сериалом/эпизодом, и всё, что помечено коротким метром, — отсекается.
    """
    return resolve(film) not in EXCLUDED_FROM_MOVIE_FLOW
