"""Какое изображение годится для полноэкранного экрана подбора.

Правило одно: горизонтальным кадром объявляется только то, что ДОКАЗАНО
горизонтально и достаточно крупно. Всё остальное честно уходит в запасной
режим «вертикальный постер на размытом фоне» — этот режим выглядит осознанно,
а растянутый на всю ширину постер выглядит как поломка.

Отбор детерминированный: одинаковый вход всегда даёт одинаковый выход. Иначе
два соседних прогона бекфила давали бы разные кадры одному фильму, и понять,
что именно изменилось, было бы нечем.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from fanart import FanartImage

HERO_POLICY_VERSION = "hero-policy-v1"

HERO_BACKDROP = "backdrop"
HERO_POSTER_BLUR = "poster_blur"

SOURCE_FANART = "fanart"
SOURCE_KINOPOISK = "kinopoisk"
SOURCE_POSTER = "poster"

HERO_TYPES = frozenset({HERO_BACKDROP, HERO_POSTER_BLUR})
HERO_SOURCES = frozenset({SOURCE_FANART, SOURCE_KINOPOISK, SOURCE_POSTER})

# Жёсткие пороги — до всякого скоринга. Кадр меньше этого на широком блоке
# заметно мылит, а соотношение вне диапазона либо обрежется до неузнаваемости,
# либо оставит пустые поля.
MIN_FANART_HERO_SCORE = 0.72
MIN_HERO_WIDTH = 1600
MIN_HERO_HEIGHT = 850
MIN_HERO_RATIO = 1.55
MAX_HERO_RATIO = 2.20

_POSTER_FALLBACK_SCORE = 0.5


@dataclass(frozen=True)
class HeroSelection:
    url: str
    hero_type: str
    source: str
    quality_score: float
    width: int | None
    height: int | None
    policy_version: str = HERO_POLICY_VERSION


def _ratio_score(width: int, height: int) -> float:
    if width <= 0 or height <= 0:
        return 0.0
    distance = abs(width / height - (16 / 9))
    if distance <= 0.03:
        return 1.0
    if distance <= 0.08:
        return 0.8
    if distance <= 0.18:
        return 0.4
    return 0.0


def _resolution_score(width: int, height: int) -> float:
    if width >= 3840 and height >= 1600:
        return 1.0
    if width >= 1920 and height >= 1000:
        return 0.9
    if width >= 1600 and height >= 850:
        return 0.72
    if width >= 1280 and height >= 700:
        return 0.5
    if width * height >= 900_000:
        return 0.3
    return 0.0


def _likes_score(likes: int) -> float:
    if likes >= 20:
        return 1.0
    if likes >= 10:
        return 0.8
    if likes >= 5:
        return 0.55
    if likes >= 2:
        return 0.3
    return 0.1


def _language_score(language: str) -> float:
    """Кадр без текста безопаснее всего: интерфейс двуязычный, а вшитый в
    картинку английский заголовок в русском интерфейсе выглядит чужеродно."""
    value = (language or "00").strip().lower() or "00"
    if value == "00":
        return 1.0
    if value in {"ru", "en"}:
        return 0.6
    return 0.25


def score_fanart_background(image: FanartImage) -> float:
    # Разрешение и композиция важнее популярности: лайки говорят о вкусе
    # загрузившего, а не о пригодности файла под широкий блок.
    score = (_resolution_score(image.width, image.height) * 0.45
             + _ratio_score(image.width, image.height) * 0.30
             + _likes_score(image.likes) * 0.15
             + _language_score(image.language) * 0.10)
    return round(max(0.0, min(1.0, score)), 4)


def qualifies(image: FanartImage) -> bool:
    if image.width < MIN_HERO_WIDTH or image.height < MIN_HERO_HEIGHT:
        return False
    if not MIN_HERO_RATIO <= image.width / image.height <= MAX_HERO_RATIO:
        return False
    return score_fanart_background(image) >= MIN_FANART_HERO_SCORE


def choose_fanart_background(images: Iterable[FanartImage]) -> HeroSelection | None:
    candidates = [(score_fanart_background(image), image)
                  for image in images if qualifies(image)]
    if not candidates:
        return None
    # Ключ полностью определён данными и заканчивается id — при равном счёте
    # выбор всё равно один и тот же, а не «какой первым попался».
    candidates.sort(key=lambda pair: (pair[0], pair[1].likes,
                                      pair[1].width * pair[1].height, pair[1].id),
                    reverse=True)
    score, best = candidates[0]
    return HeroSelection(url=best.url, hero_type=HERO_BACKDROP, source=SOURCE_FANART,
                         quality_score=score, width=best.width, height=best.height)


def poster_fallback(poster_url: str | None) -> HeroSelection | None:
    url = str(poster_url or "").strip()
    if not url:
        return None
    # Размеры намеренно пустые: это вертикальный постер, и объявлять его
    # шириной/высотой горизонтального кадра было бы неправдой.
    return HeroSelection(url=url, hero_type=HERO_POSTER_BLUR, source=SOURCE_POSTER,
                         quality_score=_POSTER_FALLBACK_SCORE, width=None, height=None)


def choose_hero(*, fanart_images: Iterable[FanartImage],
                poster_url: str | None) -> HeroSelection | None:
    """Старый kinopoisk `backdrop_url` здесь сознательно НЕ участвует.

    Ровно его непроверенность и была исходным дефектом: поле есть, а что за
    ним — неизвестно. Источник `kinopoisk` остаётся допустимым значением в
    схеме, чтобы позже подключить его, когда появятся реальные размеры.
    """
    return choose_fanart_background(fanart_images) or poster_fallback(poster_url)


def hero_payload(film: dict) -> dict:
    """Нормализованные поля для API. Строка каталога без hero_* — не ошибка:
    так выглядит любой фильм до бекфила, и он обязан работать."""
    hero_url = str(film.get("hero_url") or "").strip()
    hero_type = film.get("hero_type")
    if hero_url and hero_type in HERO_TYPES:
        score = film.get("hero_quality_score")
        return {"hero_url": hero_url, "hero_type": hero_type,
                "hero_source": film.get("hero_source"),
                "hero_quality_score": round(float(score), 4) if score is not None else None}
    poster_url = str(film.get("poster_url") or "").strip()
    if not poster_url:
        return {"hero_url": None, "hero_type": None,
                "hero_source": None, "hero_quality_score": None}
    return {"hero_url": poster_url, "hero_type": HERO_POSTER_BLUR,
            "hero_source": SOURCE_POSTER, "hero_quality_score": _POSTER_FALLBACK_SCORE}
