"""Русские хелперы отображения (перенесены из movie_bot/utils.py, без telegram-зависимостей)."""
import html
import re
from datetime import datetime, timezone


def esc(text) -> str:
    return html.escape(str(text)) if text else ""


# Перевод жанров OMDb на русский (kinopoisk отдаёт жанры сразу по-русски).
GENRE_RU = {
    "Action": "боевик", "Adventure": "приключения", "Animation": "анимация",
    "Biography": "биография", "Comedy": "комедия", "Crime": "криминал",
    "Documentary": "документальный", "Drama": "драма", "Family": "семейный",
    "Fantasy": "фэнтези", "Film-Noir": "нуар", "History": "история",
    "Horror": "ужасы", "Music": "музыка", "Musical": "мюзикл",
    "Mystery": "детектив", "Romance": "мелодрама", "Sci-Fi": "фантастика",
    "Sport": "спорт", "Thriller": "триллер", "War": "военный",
    "Western": "вестерн", "News": "новости", "Short": "короткометражка",
    "Reality-TV": "реалити", "Talk-Show": "ток-шоу", "Game-Show": "шоу",
    "Adult": "для взрослых",
}


def translate_genres(genres) -> str:
    """«Crime, Drama» → «криминал, драма». Неизвестные оставляет как есть."""
    if not genres:
        return ""
    parts = [g.strip() for g in str(genres).split(",") if g.strip()]
    return ", ".join(GENRE_RU.get(g, g) for g in parts)


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русская форма слова по числу: 1 фильм, 2 фильма, 5 фильмов."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


_MONTHS_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def human_date(iso_str: str | None) -> str:
    """ISO-дата → «29 июня» (год добавляется, только если не текущий)."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return iso_str[:10]
    out = f"{dt.day} {_MONTHS_GEN[dt.month]}"
    if dt.year != datetime.now(timezone.utc).year:
        out += f" {dt.year}"
    return out


def compact_votes(votes) -> str:
    """«1,074,757» → «1.1M», «350836» → «351K»."""
    digits = re.sub(r"\D", "", str(votes or ""))
    if not digits:
        return ""
    n = int(digits)
    if n >= 1_000_000:
        s = f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}K"
    return str(n)


def stars(rating) -> str:
    """Оценка 1–10 → 5 звёзд (★/☆)."""
    if not rating:
        return "☆☆☆☆☆"
    full = max(0, min(5, int(rating / 2 + 0.5)))
    return "★" * full + "☆" * (5 - full)


def progress_bar(pct: int, segments: int = 10) -> str:
    """Процент → шкала ▰▰▰▱▱."""
    filled = max(0, min(segments, round(pct / 100 * segments)))
    return "▰" * filled + "▱" * (segments - filled)
