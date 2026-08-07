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

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fanart import FanartImage

HERO_POLICY_VERSION = "hero-policy-v3"

HERO_BACKDROP = "backdrop"
HERO_POSTER_BLUR = "poster_blur"

SOURCE_FANART = "fanart"
SOURCE_KINOPOISK = "kinopoisk"
SOURCE_POSTER = "poster"

HERO_TYPES = frozenset({HERO_BACKDROP, HERO_POSTER_BLUR})
HERO_SOURCES = frozenset({SOURCE_FANART, SOURCE_KINOPOISK, SOURCE_POSTER})
HERO_FIT_CONTAIN = "contain"
HERO_FIT_COVER = "cover"
HERO_FITS = frozenset({HERO_FIT_CONTAIN, HERO_FIT_COVER})
DEFAULT_HERO_FIT = HERO_FIT_CONTAIN
DEFAULT_HERO_FOCUS_X = 0.5
DEFAULT_HERO_FOCUS_Y = 0.36

# Жёсткие пороги — до всякого скоринга. Кадр меньше этого на широком блоке
# заметно мылит, а соотношение вне диапазона либо обрежется до неузнаваемости,
# либо оставит пустые поля.
MIN_FANART_HERO_SCORE = 0.72
MIN_HERO_WIDTH = 1600
MIN_HERO_HEIGHT = 850
MIN_HERO_RATIO = 1.55
MAX_HERO_RATIO = 2.20

# Отдельный порог для kinopoisk, и это не послабление ради послабления.
# Fanart раздаёт РЕДАКТОРСКИЕ фоны — их отбирали люди под широкий блок, и
# требовать от них 16:9 честно. Kinopoisk раздаёт КАДРЫ ИЗ ФИЛЬМА, а кино
# снимают в 2.39:1: настоящий кадр «F1» — 1920x804, и прежний потолок 2.20
# отбраковывал его как брак. Это была не защита качества, а отказ от
# единственного горизонтального изображения, которое у фильма есть.
MIN_KINOPOISK_HERO_WIDTH = 1200
MIN_KINOPOISK_HERO_HEIGHT = 630
MIN_KINOPOISK_HERO_PIXELS = 750_000
MIN_KINOPOISK_HERO_RATIO = 1.45
MAX_KINOPOISK_HERO_RATIO = 2.55

_POSTER_FALLBACK_SCORE = 0.5

# Как часто фильму без доказанной картинки разрешено снова беспокоить провайдер
# из фонового обогащения карточки. Доказанный кадр сюда не попадает вообще.
VISUAL_RECHECK_DAYS = 14


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


# Порог тот же, что у Fanart: «достаточно хорошо для широкого блока» не зависит
# от того, кто отдал файл.
MIN_KINOPOISK_HERO_SCORE = 0.72


def kinopoisk_hero_qualifies(width: int, height: int) -> bool:
    """Жёсткий порог допуска. Именно он решает «кадр или не кадр»."""
    if width < MIN_KINOPOISK_HERO_WIDTH or height < MIN_KINOPOISK_HERO_HEIGHT:
        return False
    if width * height < MIN_KINOPOISK_HERO_PIXELS:
        return False
    return MIN_KINOPOISK_HERO_RATIO <= width / height <= MAX_KINOPOISK_HERO_RATIO


def score_kinopoisk_background(width: int, height: int) -> float:
    """Оценка НЕ решает допуск — его решает kinopoisk_hero_qualifies.

    Здесь считается только «насколько кадр хорош среди допущенных»: от этого
    зависит, как скоро мы вернёмся его перепроверять (см. _HERO_STRONG_SCORE).
    Соотношение в счёт не идёт намеренно: диапазон 1.45–2.55 уже задан
    политикой, и наказывать кадр за то, что он снят в 2.39:1, значило бы
    считать браком нормальную кинематографическую композицию.
    """
    if not kinopoisk_hero_qualifies(width, height):
        return 0.0
    pixels = width * height
    resolution = min(1.0, 0.72 + (pixels - MIN_KINOPOISK_HERO_PIXELS) / 2_000_000)
    score = resolution * 0.65 + 0.35
    return round(max(0.0, min(1.0, score)), 4)


# Kinopoisk сохраняет ссылку с директивой размера в конце пути. 1344x756 —
# это НЕ единственная доступная редакция файла, а всего лишь то, что вернул API:
# тот же CDN по тому же пути отдаёт и 1920x1080, и orig. Замер на проде:
# 1344x756 → 383 КБ, 1920x1080 → 696 КБ (настоящие 1920x1080), orig → 1.7 МБ
# (3840x2160). orig для мобильного экрана слишком тяжёлый, поэтому просим 1920.
#
# Это не новый источник и не новое API: тот же хост, тот же файл, другая
# директива. Решение всё равно принимает заголовок скачанного изображения.
_RENDITION_HOST = "avatars.mds.yandex.net"
_PREFERRED_RENDITION = "1920x1080"
_RENDITION_SUFFIX = re.compile(r"/\d{3,4}x\d{3,4}$")


def kinopoisk_rendition_candidates(url: str | None) -> list[str]:
    """Ссылки на ОДИН и тот же кадр, от предпочтительного размера к исходному."""
    normalized = str(url or "").strip()
    if not normalized:
        return []
    if _RENDITION_HOST not in normalized or not _RENDITION_SUFFIX.search(normalized):
        return [normalized]
    upgraded = _RENDITION_SUFFIX.sub("/" + _PREFERRED_RENDITION, normalized)
    return [upgraded] if upgraded == normalized else [upgraded, normalized]


def choose_kinopoisk_background(url: str | None, *, width: int | None,
                                height: int | None) -> HeroSelection | None:
    """Размеры здесь приходят ИЗ САМОГО ФАЙЛА, а не из поля каталога.

    Наличие backdrop_url по-прежнему ничего не доказывает — доказывает только
    скачанный и распознанный заголовок изображения. Нет размеров — нет кадра.
    """
    normalized = str(url or "").strip()
    if not normalized or not width or not height:
        return None
    if not kinopoisk_hero_qualifies(width, height):
        return None
    score = score_kinopoisk_background(width, height)
    if score < MIN_KINOPOISK_HERO_SCORE:
        return None
    return HeroSelection(url=normalized, hero_type=HERO_BACKDROP, source=SOURCE_KINOPOISK,
                         quality_score=score, width=width, height=height)


KINOPOISK_IMAGE_TYPE_PRIORITY = {
    "backdrops": 5,
    "frame": 4,
    "still": 4,
    "screenshot": 3,
    "wallpaper": 2,
}


def kinopoisk_image_metadata_rank(candidate: dict) -> tuple:
    """Порядок ПЕРЕБОРА кандидатов, а не решение о годности.

    Размеры здесь пришли от API и вполне могут врать — поэтому ключ нужен
    только чтобы начинать с наиболее многообещающего и чтобы два прогона по
    одному и тому же ответу дали один и тот же выбор. Последний элемент ключа —
    сам URL: при полном равенстве метаданных выбор всё равно определён.
    """
    width = int(candidate.get("width") or 0)
    height = int(candidate.get("height") or 0)
    ratio = width / height if width and height else 0.0
    return (
        KINOPOISK_IMAGE_TYPE_PRIORITY.get(candidate.get("type"), 0),
        width * height,
        width,
        -abs(ratio - (16 / 9)),
        str(candidate.get("url") or ""),
    )


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
    """Только Fanart и запасной постер.

    Kinopoisk сюда не входит намеренно: его кадр можно выбрать лишь ПОСЛЕ
    скачивания файла, а эта функция чисто вычислительная и в сеть не ходит.
    Порядок источников целиком живёт в enrichment/hero.py.
    """
    return choose_fanart_background(fanart_images) or poster_fallback(poster_url)


def _clamped_focus(value: object, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    return min(1.0, max(0.0, parsed))


def hero_presentation(film: dict) -> dict:
    """Resolve presentation separately from the selected media source."""
    if film.get("hero_type") != HERO_BACKDROP:
        return {"hero_fit": None, "hero_focus_x": None, "hero_focus_y": None}
    fit = film.get("hero_fit")
    if fit not in HERO_FITS:
        fit = DEFAULT_HERO_FIT
    return {
        "hero_fit": fit,
        "hero_focus_x": _clamped_focus(
            film.get("hero_focus_x"), DEFAULT_HERO_FOCUS_X),
        "hero_focus_y": _clamped_focus(
            film.get("hero_focus_y"), DEFAULT_HERO_FOCUS_Y),
    }


def poster_is_rejected(film: dict) -> bool:
    """Reject only the exact provider file an editor reviewed."""
    poster_url = str(film.get("poster_url") or "").strip()
    moderated_url = str(film.get("poster_display_url") or "").strip()
    return (
        bool(poster_url)
        and film.get("poster_display_state") == "rejected"
        and moderated_url == poster_url
    )


def display_poster_url(film: dict) -> str | None:
    """Public poster URL after exact-file moderation (DB value stays intact)."""
    if poster_is_rejected(film):
        return None
    value = str(film.get("poster_url") or "").strip()
    return value or None


def hero_is_verified_backdrop(film: dict) -> bool:
    """Есть ли у фильма кадр, ДОКАЗАННЫЙ проверкой файла.

    Заполненный backdrop_url этого не доказывает: он приходит из карточки
    фильма и не проверялся ничем. Доказательством считается только запись,
    которую оставил подбор кадра, — она делается после скачивания файла.
    """
    return (film.get("hero_type") == HERO_BACKDROP
            and bool(str(film.get("hero_url") or "").strip()))


def visual_repair_needed(film: dict, *, now: datetime | None = None) -> bool:
    """Стоит ли фоново сходить к провайдеру за картинками этого фильма.

    Прежнее правило («url пуст И проверки не было») делало ЛЮБУЮ непустую
    ссылку вечной: битый постер и заглушка вместо кадра считались готовым
    результатом и больше никогда не пересматривались.

    Здесь наоборот: критерий — не «пусто ли поле», а «есть ли пригодная
    картинка». При этом доказанный кадр закрывает вопрос совсем, а всё
    остальное перепроверяется не чаще, чем раз в окно, — иначе каждое открытие
    карточки уходило бы во внешний API.
    """
    moment = now or datetime.now(UTC)
    if display_poster_url(film) is None and not _checked_recently(
            film.get("poster_checked_at"), moment):
        return True
    if hero_is_verified_backdrop(film):
        return False
    return not _checked_recently(film.get("artwork_checked_at"), moment)


def _checked_recently(checked_at: object, moment: datetime) -> bool:
    value = str(checked_at or "").strip()
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # Нераспознанная отметка — это НЕ «проверяли только что». Считаем, что
        # проверки не было: лишний фоновый запрос дешевле застрявшей картинки.
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return moment - parsed < timedelta(days=VISUAL_RECHECK_DAYS)


def public_media_row(film: dict) -> dict:
    """Resolve public media without exposing moderation audit fields."""
    public = dict(film)
    for field in (
        "poster_display_state",
        "poster_display_url",
        "poster_reject_reason",
    ):
        public.pop(field, None)
    public["poster_url"] = display_poster_url(film)
    public.update(hero_payload(film))
    return public


def hero_payload(film: dict) -> dict:
    """Нормализованные поля для API. Строка каталога без hero_* — не ошибка:
    так выглядит любой фильм до бекфила, и он обязан работать."""
    hero_url = str(film.get("hero_url") or "").strip()
    hero_type = film.get("hero_type")
    # Сохранённый URL — источник правды только для КАДРА: он доказан проверкой
    # файла, и другого такого же кадра у нас нет. Запасной режим — это решение
    # «показывать постер», а не копия ссылки на него: постер живёт своей жизнью
    # и заменяется на лучший (см. upgrade_film_poster). Замороженная копия
    # означала бы, что экран подбора неделями показывает старый постер, пока
    # весь остальной продукт показывает новый.
    if (hero_url and hero_type == HERO_BACKDROP):
        score = film.get("hero_quality_score")
        return {
            "hero_url": hero_url,
            "hero_type": hero_type,
            "hero_source": film.get("hero_source"),
            "hero_quality_score": round(float(score), 4) if score is not None else None,
            **hero_presentation(film),
        }
    poster_url = str(film.get("poster_url") or "").strip()
    if not poster_url or poster_is_rejected(film):
        return {"hero_url": None, "hero_type": None,
                "hero_source": None, "hero_quality_score": None,
                "hero_fit": None, "hero_focus_x": None, "hero_focus_y": None}
    return {"hero_url": poster_url, "hero_type": HERO_POSTER_BLUR,
            "hero_source": SOURCE_POSTER, "hero_quality_score": _POSTER_FALLBACK_SCORE,
            "hero_fit": None, "hero_focus_x": None, "hero_focus_y": None}
