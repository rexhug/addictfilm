"""Value-level helpers shared by every repository: timestamps, genre
canonicalisation, packed person/portrait columns and the pair identity strings.

None of these touch the database.  They are here so that splitting the query
layer by domain does not force each domain to carry its own copy of the genre
canon — two copies is exactly how "драма" and "Drama" became separate rows in
the first place.
"""
import json
import re
from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Каталог наполняется из kinopoisk (жанры по-русски) и OMDb (по-английски) — из-за
# этого в film_genres встречаются дубли одного жанра на двух языках («драма» и
# «Drama»). Приводим к единому русскому канону: при записи (см. _split_genres) и
# при чтении (list_genres мёржит, browse_by_genre матчит все алиасы) — без миграции.
_GENRE_CANON: dict[str, str] = {
    "drama": "драма", "comedy": "комедия", "thriller": "триллер", "action": "боевик",
    "crime": "криминал", "mystery": "детектив", "detective": "детектив", "horror": "ужасы",
    "sci-fi": "фантастика", "science fiction": "фантастика", "fantasy": "фэнтези",
    "adventure": "приключения", "biography": "биография", "history": "история",
    "romance": "мелодрама", "melodrama": "мелодрама", "sport": "спорт", "war": "военный",
    "documentary": "документальный", "family": "семейный", "animation": "мультфильм",
    "cartoon": "мультфильм", "anime": "мультфильм", "аниме": "мультфильм",
    "детский": "семейный", "kids": "семейный", "children": "семейный",
    "western": "приключения", "вестерн": "приключения", "musical": "мюзикл",
    "music": "мюзикл", "музыка": "мюзикл", "film-noir": "криминал", "фильм-нуар": "криминал",
    "short": "короткометражка",
}


# Продуктовая витрина жанров: только осмысленные top-level жанры каталога.
# Форматы («короткометражка»), ТВ-мусор («реальное ТВ») и редкие хвосты
# («спорт», «мюзикл») в витрину не попадают — фильмы при этом остаются
# доступными через поиск и карточки. Сортировка по count делается в list_genres.
_GENRE_SHOWCASE = frozenset((
    "драма", "боевик", "комедия", "триллер", "приключения", "фантастика",
    "криминал", "детектив", "фэнтези", "ужасы", "мультфильм", "мелодрама",
    "семейный", "биография", "военный", "история", "документальный",
))


def _canon_genre(genre: str | None) -> str:
    """Единый (русский) канон жанра; неизвестные значения — как есть (обрезанные)."""
    s = (genre or "").strip()
    return _GENRE_CANON.get(s.casefold(), s)


def _genre_query_aliases(genre: str) -> list[str]:
    """Все варианты написания жанра (рус+англ, lower) для матча в film_genres."""
    canon = _canon_genre(genre)
    aliases = {canon.casefold(), (genre or "").strip().casefold()}
    aliases.update(en for en, ru in _GENRE_CANON.items() if ru == canon)
    return [a for a in aliases if a]


def _split_genres(value: str | None) -> list[str]:
    """Genre values for the derived, indexable film_genres table. Храним КАК ЕСТЬ
    (kinopoisk даёт рус., OMDb — англ.); к единому канону приводим только при
    чтении (list_genres мёржит, browse_by_genre матчит все алиасы) — без миграции
    и без риска задвоить счётчики при повторном бэкфилле проекции."""
    return list(dict.fromkeys(
        genre.strip() for genre in (value or "").split(",")
        if genre.strip() and genre.strip() != "N/A"
    ))


def _split_people(value: str | None) -> list[str]:
    """Return the canonical comma-separated person names stored on a film."""
    return list(dict.fromkeys(name.strip() for name in (value or "").split(",") if name.strip()))


def _person_key(name: str | None) -> str:
    """Stable, forgiving key for joining names from two catalog providers."""
    return re.sub(r"\s+", " ", str(name or "").strip()).casefold()


def _portrait_urls(entry: object) -> list[str]:
    """Read a primary portrait and its source fallbacks from one JSON entry."""
    if not isinstance(entry, dict):
        return []
    values = [entry.get("photo_url"), *(entry.get("fallback_photo_urls") or [])]
    result: list[str] = []
    for value in values:
        url = str(value or "").strip()
        if url.startswith(("https://", "http://")) and url not in result:
            result.append(url)
    return result


def _portrait_entries(value: str | None) -> list[dict]:
    """Tolerantly decode the stored people portrait payload."""
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [entry for entry in decoded if isinstance(entry, dict)] if isinstance(decoded, list) else []


# ── Каталог фильмов ──────────────────────────────────────────────────────────
def _catalog_search_text(title: str | None, title_original: str | None,
                         actors: str | None, directors: str | None,
                         imdb_id: str, kp_id: str | None) -> str:
    """Unicode-safe lookup text stored once with a catalog record. Включает
    актёров и режиссёров — поиск по людям работает без внешних запросов."""
    values = (title, title_original, actors, directors, imdb_id, kp_id)
    return " ".join(" ".join(str(value or "").split()).casefold() for value in values).strip()


def _pair_key(first_user_id: int, second_user_id: int) -> str:
    """Stable identity of one exact two-person pair, independent of sessions."""
    low, high = sorted((int(first_user_id), int(second_user_id)))
    return f"pair:{low}:{high}"


def _legacy_pair_session_id(pair_key: str, started_at: str) -> str:
    return f"session:{pair_key}:{started_at}"


def _parse_legacy_pair_id(pair_id: str | None) -> tuple[int, int, str] | None:
    """Read the old ``pair:<low>:<high>:<since>`` event identity safely."""
    if not pair_id or not pair_id.startswith("pair:"):
        return None
    parts = pair_id.split(":", 3)
    if len(parts) != 4:
        return None
    try:
        first_user_id, second_user_id = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if first_user_id >= second_user_id or not parts[3]:
        return None
    return first_user_id, second_user_id, parts[3]
