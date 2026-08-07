"""Язык метаданных каталога: определение, жанры, отбор значений под язык UI.

Продукт поддерживает ровно два языка — ru и en. Отдельной таблицы переводов нет
намеренно: на двух языках она дала бы join на каждом чтении каталога ради того,
что помещается в восемь колонок.

Здесь НЕТ перевода. Определение языка нужно только затем, чтобы разобрать уже
накопленные данные: провайдеры годами писали в одни и те же колонки то русский,
то английский, а иногда и украинский текст, и отличить «пусто» от «лежит не то»
иначе нечем. Новые записи раскладываются по языковым колонкам при загрузке.
"""
from __future__ import annotations

import re

SUPPORTED_LANGUAGES = ("ru", "en")
DEFAULT_LANGUAGE = "ru"

# Буквы, которых в русском алфавите нет вовсе. Одной такой достаточно, чтобы
# строка не считалась русской: именно так украинское описание попадало в
# карточку с русским интерфейсом и выглядело как «перевод сломался».
_UKRAINIAN_ONLY = frozenset("іїєґІЇЄҐ")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[A-Za-z]")


def normalize_language(value: object) -> str:
    """Код языка из чего угодно. Неизвестное — русский, а не исключение."""
    code = str(value or "").strip().lower()[:2]
    return code if code in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def looks_ukrainian(value: object) -> bool:
    return any(ch in _UKRAINIAN_ONLY for ch in str(value or ""))


def looks_russian(value: object) -> bool:
    """Кириллица есть, украинских букв нет.

    Проверка намеренно грубая: она отвечает на вопрос «можно ли положить это в
    русский слот», а не «на каком языке написан текст». Ошибка в сторону «нет»
    стоит одного лишнего запроса к провайдеру; ошибка в сторону «да» — это
    украинское описание в русской карточке, то есть ровно тот баг.
    """
    text = str(value or "").strip()
    if not text or looks_ukrainian(text):
        return False
    return bool(_CYRILLIC.search(text))


def looks_english(value: object) -> bool:
    """Латиница есть, кириллицы нет."""
    text = str(value or "").strip()
    if not text:
        return False
    return bool(_LATIN.search(text)) and not _CYRILLIC.search(text)


def accepts(value: object, language: str) -> bool:
    """Годится ли строка для слота этого языка."""
    return looks_russian(value) if normalize_language(language) == "ru" else looks_english(value)


# ── жанры ────────────────────────────────────────────────────────────────
# Канон в каталоге русский (см. db_helpers._GENRE_CANON), поэтому здесь только
# обратная сторона: канон → английская витрина. Ходить к провайдеру за жанрами
# не нужно ни на одном языке.
GENRE_DISPLAY_EN: dict[str, str] = {
    "драма": "Drama", "боевик": "Action", "комедия": "Comedy", "триллер": "Thriller",
    "приключения": "Adventure", "фантастика": "Sci-Fi", "криминал": "Crime",
    "детектив": "Mystery", "фэнтези": "Fantasy", "ужасы": "Horror",
    "мультфильм": "Animation", "мелодрама": "Romance", "семейный": "Family",
    "биография": "Biography", "военный": "War", "история": "History",
    "документальный": "Documentary", "спорт": "Sport", "мюзикл": "Musical",
    "короткометражка": "Short", "реальное тв": "Reality TV", "ток-шоу": "Talk Show",
    "новости": "News", "концерт": "Concert", "игра": "Game Show", "церемония": "Ceremony",
    "аниме": "Animation", "вестерн": "Western", "фильм-нуар": "Film-Noir",
}


def localized_genres(genres: object, language: str, *, canon=None) -> str:
    """Строка жанров под язык UI.

    Фильтрация жанров не меняется — она живёт на film_genres и работает по
    канону. Здесь только ВИТРИНА. Незнакомый жанр отдаём как есть: показать
    непривычное слово честнее, чем потерять его вместе с фильмом.
    """
    from db_helpers import _canon_genre

    resolve = canon or _canon_genre
    language = normalize_language(language)
    names: list[str] = []
    for raw in str(genres or "").split(","):
        item = " ".join(raw.split())
        if not item:
            continue
        canonical = resolve(item)
        names.append(canonical if language == "ru" else GENRE_DISPLAY_EN.get(
            canonical.casefold(), canonical))
    # dict.fromkeys — дедуп с сохранением порядка: «Sci-Fi, фантастика» из
    # смешанной легаси-строки схлопывается в один жанр, а не показывается дважды.
    return ", ".join(dict.fromkeys(names))


# ── отбор значения под язык ──────────────────────────────────────────────
_LOCALIZED_FIELDS = ("title", "plot", "genres", "directors")


def localized_value(film: dict, field: str, language: str) -> str | None:
    """Значение поля под язык, с честным откатом на легаси-колонку.

    Порядок: колонка нужного языка → колонка второго языка, если она подходит
    по алфавиту → общая легаси-колонка. Пустая строка и None равнозначны.
    """
    language = normalize_language(language)
    other = "en" if language == "ru" else "ru"
    for key in (f"{field}_{language}", f"{field}_{other}", field):
        value = str(film.get(key) or "").strip()
        if not value:
            continue
        if key.endswith(f"_{other}"):
            # Второй язык берём только если он и правда на нужном алфавите:
            # иначе это просто ещё одна копия не того текста.
            if not accepts(value, language):
                continue
        return value
    return None


def localized_movie_payload(film: dict, language: str) -> dict:
    """Плоская карточка под один язык. Используется бекфилом и тестами —
    рантайм-выдача остаётся сырой, язык выбирает фронтенд."""
    language = normalize_language(language)
    payload = {field: localized_value(film, field, language) for field in _LOCALIZED_FIELDS}
    payload["genres"] = localized_genres(
        payload["genres"] or film.get("genres"), language) or None
    payload["language"] = language
    return payload
