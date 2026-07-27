"""Проверенные соответствия «данные провайдера → канонический словарь».

Здесь и только здесь живут строки провайдеров. Ни одно значение от провайдера
не становится публичным тегом само по себе: незнакомый жанр или ключевое слово
попадает в диагностику, но не в выдачу.

Сопоставление ВСЕГДА по нормализованной метке целиком или по границе слова.
Подстроки уже стоили продукту двух дефектов: «черн» находилось в «Чернике», а
«мест» — в «вместе».
"""
from __future__ import annotations

import re
import unicodedata

from .taxonomy import AudienceFlag, CanonicalGenre, CanonicalTheme, CanonicalTone


def normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = re.sub(r"[^\w\s-]", " ", normalized)
    return " ".join(normalized.split())


# ── Жанры провайдеров → канон ────────────────────────────────────────────────
# kinopoisk отдаёт русские названия, OMDb — английские; каталог хранит как есть.
PROVIDER_GENRE_TO_CANONICAL: dict[str, str] = {
    "боевик": CanonicalGenre.ACTION, "action": CanonicalGenre.ACTION,
    "приключения": CanonicalGenre.ADVENTURE, "adventure": CanonicalGenre.ADVENTURE,
    "вестерн": CanonicalGenre.ADVENTURE, "western": CanonicalGenre.ADVENTURE,
    "мультфильм": CanonicalGenre.ANIMATION, "animation": CanonicalGenre.ANIMATION,
    "аниме": CanonicalGenre.ANIMATION, "anime": CanonicalGenre.ANIMATION,
    "биография": CanonicalGenre.BIOGRAPHY, "biography": CanonicalGenre.BIOGRAPHY,
    "комедия": CanonicalGenre.COMEDY, "comedy": CanonicalGenre.COMEDY,
    "криминал": CanonicalGenre.CRIME, "crime": CanonicalGenre.CRIME,
    "фильм-нуар": CanonicalGenre.CRIME, "film-noir": CanonicalGenre.CRIME,
    "документальный": CanonicalGenre.DOCUMENTARY, "documentary": CanonicalGenre.DOCUMENTARY,
    "драма": CanonicalGenre.DRAMA, "drama": CanonicalGenre.DRAMA,
    "семейный": CanonicalGenre.FAMILY, "family": CanonicalGenre.FAMILY,
    "детский": CanonicalGenre.FAMILY, "kids": CanonicalGenre.FAMILY,
    "фэнтези": CanonicalGenre.FANTASY, "fantasy": CanonicalGenre.FANTASY,
    "история": CanonicalGenre.HISTORY, "history": CanonicalGenre.HISTORY,
    "ужасы": CanonicalGenre.HORROR, "horror": CanonicalGenre.HORROR,
    "мюзикл": CanonicalGenre.MUSIC, "musical": CanonicalGenre.MUSIC,
    "музыка": CanonicalGenre.MUSIC, "music": CanonicalGenre.MUSIC,
    "детектив": CanonicalGenre.MYSTERY, "mystery": CanonicalGenre.MYSTERY,
    "detective": CanonicalGenre.MYSTERY,
    "мелодрама": CanonicalGenre.ROMANCE, "romance": CanonicalGenre.ROMANCE,
    "melodrama": CanonicalGenre.ROMANCE,
    "фантастика": CanonicalGenre.SCIENCE_FICTION, "sci-fi": CanonicalGenre.SCIENCE_FICTION,
    "science fiction": CanonicalGenre.SCIENCE_FICTION,
    "триллер": CanonicalGenre.THRILLER, "thriller": CanonicalGenre.THRILLER,
    "военный": CanonicalGenre.WAR, "war": CanonicalGenre.WAR,
}

# Жанры-форматы: это не про содержание, а про тип записи.
FORMAT_GENRES = frozenset({"короткометражка", "short"})


def canonical_genres(names) -> tuple[frozenset[str], tuple[str, ...]]:
    """(канонические жанры, нераспознанные метки для диагностики)."""
    canonical, unknown = set(), []
    for raw in names or ():
        label = normalize_label(raw)
        if not label or label in FORMAT_GENRES:
            continue
        mapped = PROVIDER_GENRE_TO_CANONICAL.get(label)
        if mapped:
            canonical.add(str(mapped))
        else:
            unknown.append(label)
    return frozenset(canonical), tuple(unknown)


# ── Проверенные текстовые маркеры → тема/тон ─────────────────────────────────
# Полные обороты, а не корни: «чёрная комедия» — да, «чёрн» — нет.
REVIEWED_TONE_MARKERS: dict[str, tuple[str, ...]] = {
    CanonicalTone.DARK_COMEDY: (
        "чёрная комеди", "черная комеди", "чёрной комеди", "черной комеди",
        "чёрный юмор", "черный юмор", "чёрного юмора", "черного юмора",
        "чёрным юмором", "черным юмором", "чёрная сатира", "черная сатира",
        "трагикомед", "жестоким чувством юмора", "жестокое чувство юмора",
        "жестокий юмор", "злой юмор",
        "dark comedy", "black comedy", "dark humor", "dark humour", "tragicomed",
    ),
    CanonicalTone.SATIRICAL: (
        "сатир", "пароди", "высмеива", "гротеск", "издёвк", "издевк",
        "satire", "satirical", "parody",
    ),
    CanonicalTone.ABSURDIST: (
        "абсурд", "сюрреалист", "нелеп", "absurd", "surreal",
    ),
    CanonicalTone.PSYCHOLOGICAL: (
        "психологич", "рассудок", "помешат", "psycholog", "sanity",
    ),
    CanonicalTone.SLOW_BURN: (
        "медленн", "неспешн", "созерцат", "slow burn",
    ),
    CanonicalTone.COZY: (
        "уютн", "тёпл", "тепл", "душевн", "cozy", "heartwarming", "feel-good",
    ),
    CanonicalTone.BLEAK: (
        "безысходн", "беспросветн", "отчая", "bleak", "hopeless",
    ),
    CanonicalTone.DISTURBING: (
        "жесток", "изувер", "тошнотвор", "disturbing", "brutal",
    ),
    CanonicalTone.INSPIRING: (
        "вдохновля", "преодолева", "не сдаёт", "не сдает", "inspiring", "triumph",
    ),
    CanonicalTone.MELANCHOLIC: (
        "меланхол", "тоск", "грусть", "печал", "melanchol", "wistful",
    ),
    CanonicalTone.THOUGHT_PROVOKING: (
        "философ", "экзистенц", "заставляет задуматься", "смысл жизни",
        "philosoph", "existential", "thought-provoking",
    ),
    CanonicalTone.SUSPENSEFUL: (
        "напряжени", "саспенс", "suspense", "on edge",
    ),
    CanonicalTone.FEEL_GOOD: (
        "добр", "жизнеутвержд", "feel good", "uplifting",
    ),
}

REVIEWED_THEME_MARKERS: dict[str, tuple[str, ...]] = {
    CanonicalTheme.FRIENDSHIP: ("друж", "товарищ", "friendship"),
    CanonicalTheme.FAMILY_RELATIONSHIP: ("семь", "семей", "родител", "отц", "матер",
                                         "family", "father", "mother"),
    CanonicalTheme.REVENGE: ("месть", "мести", "мстит", "отомст", "возмезди", "revenge"),
    CanonicalTheme.INVESTIGATION: ("расслед", "улик", "следовател", "детектив", "investigat"),
    CanonicalTheme.SURVIVAL: ("выжив", "катастроф", "спасен", "surviv", "disaster"),
    CanonicalTheme.COMING_OF_AGE: ("взросл", "юност", "подрост", "coming of age"),
    CanonicalTheme.HEIST: ("ограблени", "налёт", "налет", "heist", "robbery"),
    CanonicalTheme.POLITICAL_CONFLICT: ("политич", "переворот", "революци", "political"),
    CanonicalTheme.TIME_TRAVEL: ("путешествие во времен", "временн петл", "time travel"),
    CanonicalTheme.DYSTOPIA: ("антиутоп", "тоталитар", "dystop"),
    CanonicalTheme.GRIEF: ("утрат", "горе", "оплакив", "grief", "mourning"),
    CanonicalTheme.REDEMPTION: ("искуплени", "исправит", "redemption"),
    CanonicalTheme.SUPERNATURAL: ("сверхъест", "призрак", "демон", "мистик",
                                  "supernatural", "ghost"),
}


def _pattern(needle: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(needle)}", re.IGNORECASE)


_TONE_PATTERNS = {tone: tuple(_pattern(n) for n in needles)
                  for tone, needles in REVIEWED_TONE_MARKERS.items()}
_THEME_PATTERNS = {theme: tuple(_pattern(n) for n in needles)
                   for theme, needles in REVIEWED_THEME_MARKERS.items()}


def match_reviewed_tones(text: str) -> frozenset[str]:
    lowered = normalize_label(text)
    return frozenset(str(tone) for tone, patterns in _TONE_PATTERNS.items()
                     if any(p.search(lowered) for p in patterns))


def match_reviewed_themes(text: str) -> frozenset[str]:
    lowered = normalize_label(text)
    return frozenset(str(theme) for theme, patterns in _THEME_PATTERNS.items()
                     if any(p.search(lowered) for p in patterns))


# ── Возрастные ограничения → флаги аудитории ─────────────────────────────────
# Единственный по-настоящему надёжный источник про аудиторию у наших
# провайдеров: kinopoisk отдаёт числовой ageRating и ratingMpaa, OMDb — Rated.
MPAA_TO_AUDIENCE: dict[str, tuple[str, ...]] = {
    "g": (AudienceFlag.FAMILY_FRIENDLY, AudienceFlag.CHILD_ORIENTED),
    "pg": (AudienceFlag.FAMILY_FRIENDLY,),
    "pg-13": (),
    "r": (AudienceFlag.MATURE_THEMES,),
    "nc-17": (AudienceFlag.MATURE_THEMES,),
}


def audience_from_certification(certification: str | None, age_rating: int | None) -> frozenset[str]:
    flags: set[str] = set()
    label = normalize_label(certification or "").replace(" ", "")
    for key, mapped in MPAA_TO_AUDIENCE.items():
        if label == key.replace("-", ""):
            flags.update(str(flag) for flag in mapped)
    if age_rating is not None:
        # 12+ — это НЕ «семейное». Прежний порог помечал так «Марс атакует!»,
        # и профиль сам себе противоречил: семейный ярлык при мрачности 0.75.
        # Российская шкала: 0+/6+ — детское, 12+/16+ — подростковое (нейтрально),
        # 18+ — взрослое.
        if age_rating <= 6:
            flags.update({str(AudienceFlag.FAMILY_FRIENDLY), str(AudienceFlag.CHILD_ORIENTED)})
        elif age_rating >= 18:
            flags.add(str(AudienceFlag.MATURE_THEMES))
    return frozenset(flags)
