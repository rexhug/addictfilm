"""Стабильные коды причин рекомендации и их человеческие формулировки.

Раньше карточка показывала строку, собранную из внутренних тегов ответов:
«юмор, абсурд, сатира, a flawed lead». Это одновременно три дефекта — наружу
утекали служебные теги, английский текст попадал в русский интерфейс, и у трёх
разных фильмов стояло одно и то же объяснение, ничего не доказывающее.

Теперь движок возвращает КОДЫ, привязанные к конкретному фильму, а текст —
производная от кода. Код стабилен, его можно логировать и тестировать;
формулировка живёт отдельно и переводится и на сервере, и в интерфейсе.
"""
from __future__ import annotations

# Доказательство по смыслу запроса — самые сильные причины, идут первыми.
DARK_COMEDY_TONE = "DARK_COMEDY_TONE"
SATIRICAL_HUMOR = "SATIRICAL_HUMOR"
ABSURD_DARK_HUMOR = "ABSURD_DARK_HUMOR"
# Совпадение по измерению, которое человек назвал главным.
HIGH_TENSION = "HIGH_TENSION"
INTELLECTUAL = "INTELLECTUAL"
COZY_TONE = "COZY_TONE"
EMOTIONAL_STORY = "EMOTIONAL_STORY"
LIGHT_HUMOR = "LIGHT_HUMOR"
FAST_PACE = "FAST_PACE"
SLOW_ATMOSPHERE = "SLOW_ATMOSPHERE"
UNREAL_WORLD = "UNREAL_WORLD"
GROUNDED_STORY = "GROUNDED_STORY"
# Общие причины — только как дополнение, никогда как единственное объяснение
# смысловой рекомендации.
MATCHES_MOOD = "MATCHES_MOOD"
MATCHES_USER_TASTE = "MATCHES_USER_TASTE"
HIGH_QUALITY = "HIGH_QUALITY"
HIDDEN_GEM = "HIDDEN_GEM"
IN_WISHLIST = "IN_WISHLIST"
PAIR_FRIENDLY = "PAIR_FRIENDLY"
UNSEEN_PICK = "UNSEEN_PICK"
# Стратегии умного случайного выбора: каждая — отдельное обещание человеку.
RANDOM_RELIABLE = "RANDOM_RELIABLE"
RANDOM_TASTE_MATCH = "RANDOM_TASTE_MATCH"
RANDOM_DISCOVERY = "RANDOM_DISCOVERY"

TEXTS: dict[str, dict[str, str]] = {
    DARK_COMEDY_TONE:  {"ru": "Чёрный юмор и мрачный тон", "en": "Dark humour with a grim tone"},
    SATIRICAL_HUMOR:   {"ru": "Сатира на серьёзные темы", "en": "Satire about serious things"},
    ABSURD_DARK_HUMOR: {"ru": "Абсурдная комедия с мрачной подачей", "en": "Absurd comedy, grim delivery"},
    HIGH_TENSION:      {"ru": "Держит в напряжении", "en": "Keeps the tension up"},
    INTELLECTUAL:      {"ru": "Требует внимания и размышления", "en": "Asks for attention and thought"},
    COZY_TONE:         {"ru": "Спокойный и светлый тон", "en": "Calm, light tone"},
    EMOTIONAL_STORY:   {"ru": "Сильная эмоциональная история", "en": "A strong emotional story"},
    LIGHT_HUMOR:       {"ru": "Много юмора", "en": "Plenty of humour"},
    FAST_PACE:         {"ru": "Быстрый темп с самого начала", "en": "Fast from the first minute"},
    SLOW_ATMOSPHERE:   {"ru": "Медленная, атмосферная подача", "en": "Slow, atmospheric pacing"},
    UNREAL_WORLD:      {"ru": "Придуманный, ненастоящий мир", "en": "An invented, unreal world"},
    GROUNDED_STORY:    {"ru": "Достоверная, приземлённая история", "en": "A grounded, believable story"},
    MATCHES_MOOD:      {"ru": "Совпадает с твоим запросом", "en": "Matches what you asked for"},
    MATCHES_USER_TASTE: {"ru": "Похож на то, что тебе нравится", "en": "Close to what you usually like"},
    HIGH_QUALITY:      {"ru": "Высокая оценка зрителей", "en": "Highly rated by viewers"},
    HIDDEN_GEM:        {"ru": "Не на слуху, но крепкий", "en": "Not well known, but solid"},
    IN_WISHLIST:       {"ru": "Уже в твоём списке «Хочу посмотреть»", "en": "Already on your watchlist"},
    PAIR_FRIENDLY:     {"ru": "Подходит вам обоим", "en": "Works for both of you"},
    UNSEEN_PICK:       {"ru": "Из фильмов, которых ты ещё не видел", "en": "From films you have not seen yet"},
    RANDOM_RELIABLE:   {"ru": "Надёжный выбор", "en": "A reliable choice"},
    RANDOM_TASTE_MATCH: {"ru": "Похоже на то, что ты любишь", "en": "Close to what you love"},
    RANDOM_DISCOVERY:  {"ru": "Находка не на слуху", "en": "An off-the-radar find"},
}

KNOWN_CODES = frozenset(TEXTS)
MAX_REASONS = 3


def is_known(code: str) -> bool:
    return code in KNOWN_CODES


def sanitize(codes) -> list[str]:
    """Наружу уходят только известные коды: незнакомая строка — это утечка
    внутреннего тега, и её лучше потерять, чем показать человеку."""
    seen: list[str] = []
    for code in codes or ():
        if is_known(code) and code not in seen:
            seen.append(code)
    return seen[:MAX_REASONS]


def describe(codes, language: str) -> str:
    """Запасная строка для клиентов, которые ещё не умеют в коды."""
    locale = "en" if language == "en" else "ru"
    parts = [TEXTS[code][locale] for code in sanitize(codes)]
    if not parts:
        return "Matches your current request." if locale == "en" else "Подходит под твой запрос."
    return " · ".join(parts)
