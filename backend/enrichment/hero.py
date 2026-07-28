"""Подбор изображения для полноэкранного экрана подбора.

Работает ТОЛЬКО в воркере. На пути пользовательского запроса (рулетка, умный
случайный) внешних вызовов нет вовсе: человек нажимает кнопку и получает
результат из каталога, а не ждёт чужой сервис.

Идемпотентность держится не на отдельной таблице заданий, а на самом отборе
кандидатов: фильм со свежей отметкой hero_checked_at просто не попадает в
выборку. Второй прогон подряд не делает ни одного внешнего запроса — это
проверено тестом. Отдельный тип задания в movie_enrichment_jobs заводить не
стали намеренно: там CHECK-ограничение на job_type, и расширять его пришлось бы
блокирующей миграцией на живой таблице ради работы, у которой уже есть
естественный ключ дедупликации (film_id + отметка проверки).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import aiohttp
import database as db
import fanart
import hero_media
from config import FANART_HERO_ENABLED, HERO_REFRESH_BATCH, HERO_REFRESH_CONCURRENCY

logger = logging.getLogger(__name__)

# Проверка выбранного файла нужна ровно чтобы поймать «метаданные врут»: битую
# ссылку, html-заглушку вместо картинки, обрезанный файл. Скачивать ради
# скоринга весь каталог не нужно — размеры приходят от API.
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=12, connect=5)
_PROBE_MAX_BYTES = 12 * 1024 * 1024
_PROBE_MIN_BYTES = 2048
# Расхождение с метаданными в пределах округления допустимо, кратное — нет.
_PROBE_SIZE_TOLERANCE = 0.05

PROBE_OK = "ok"
PROBE_REJECTED = "rejected"
PROBE_UNKNOWN = "unknown"

ACTION_STORED = "stored"
ACTION_UNCHANGED = "unchanged"
ACTION_NONE = "none"
ACTION_UNAVAILABLE = "unavailable"
ACTION_DISABLED = "disabled"
ACTION_DRY_RUN = "dry_run"


@dataclass(frozen=True)
class HeroOutcome:
    film_id: int
    title: str
    action: str
    hero_type: str | None = None
    hero_source: str | None = None
    quality_score: float | None = None
    width: int | None = None
    height: int | None = None
    candidates: int = 0

    def as_dict(self) -> dict:
        return {"film_id": self.film_id, "title": self.title, "action": self.action,
                "hero_type": self.hero_type, "hero_source": self.hero_source,
                "quality_score": self.quality_score, "width": self.width,
                "height": self.height, "candidates": self.candidates}


@dataclass
class HeroReport:
    examined: int = 0
    stored: int = 0
    unchanged: int = 0
    without_hero: int = 0
    unavailable: int = 0
    outcomes: list[HeroOutcome] = field(default_factory=list)

    def add(self, outcome: HeroOutcome) -> None:
        self.examined += 1
        self.outcomes.append(outcome)
        if outcome.action == ACTION_STORED:
            self.stored += 1
        elif outcome.action == ACTION_UNCHANGED:
            self.unchanged += 1
        elif outcome.action == ACTION_NONE:
            self.without_hero += 1
        elif outcome.action == ACTION_UNAVAILABLE:
            self.unavailable += 1

    def as_dict(self) -> dict:
        return {"examined": self.examined, "stored": self.stored,
                "unchanged": self.unchanged, "without_hero": self.without_hero,
                "unavailable": self.unavailable}


def _sniff_dimensions(head: bytes) -> tuple[int, int] | None:
    """Размеры из заголовка файла, без Pillow.

    Pillow ради этой одной проверки в образ не тянем: PNG и JPEG (а Fanart
    отдаёт именно их) объявляют размеры в первых байтах. Не распознали формат —
    считаем проверку неприменимой, а не проваленной.
    """
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 9 <= len(head):
        if head[offset] != 0xFF:
            offset += 1
            continue
        marker = head[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        length = int.from_bytes(head[offset + 2:offset + 4], "big")
        if length < 2:
            return None
        # SOFn — единственные сегменты, где лежит настоящий размер кадра.
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return (int.from_bytes(head[offset + 7:offset + 9], "big"),
                    int.from_bytes(head[offset + 5:offset + 7], "big"))
        offset += 2 + length
    return None


async def probe_image(url: str, *, expected_width: int | None = None,
                      expected_height: int | None = None, session=None) -> str:
    """Проверка выбранного файла. Три исхода, а не два.

    Различие принципиальное, и оно проявилось сразу на проде: CDN Fanart лежал,
    проверка не проходила — и «не смог проверить» трактовалось как «файл плохой».
    В итоге весь каталог уезжал в запасной постер И получал свежую отметку
    проверки, то есть замораживался на недели из-за чужого получасового сбоя.

    PROBE_OK        файл получен и совпал с метаданными;
    PROBE_REJECTED  файл получен, но негоден (не картинка, обрезан, врут размеры);
    PROBE_UNKNOWN   до файла не достучались — это про сеть, а не про файл.
    """
    owns = session is None
    http = session or aiohttp.ClientSession(timeout=_PROBE_TIMEOUT)
    try:
        async with http.get(url) as response:
            # 5xx и 429 — это тоже «сейчас не смогли», а не «файл негоден».
            if response.status >= 500 or response.status == 429:
                return PROBE_UNKNOWN
            if response.status != 200:
                return PROBE_REJECTED
            if not str(response.headers.get("Content-Type", "")).lower().startswith("image/"):
                return PROBE_REJECTED
            head = b""
            size = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > _PROBE_MAX_BYTES:
                    return PROBE_REJECTED
                if len(head) < 4096:
                    head += chunk
    except (TimeoutError, aiohttp.ClientError) as exc:
        logger.info("hero: файл не удалось проверить (%s)", type(exc).__name__)
        return PROBE_UNKNOWN
    finally:
        if owns:
            await http.close()

    if size < _PROBE_MIN_BYTES:
        return PROBE_REJECTED
    sniffed = _sniff_dimensions(head)
    if sniffed and expected_width and expected_height:
        width, height = sniffed
        if (abs(width - expected_width) / expected_width > _PROBE_SIZE_TOLERANCE
                or abs(height - expected_height) / expected_height > _PROBE_SIZE_TOLERANCE):
            logger.warning("hero: файл не совпал с метаданными Fanart (%sx%s против %sx%s)",
                           width, height, expected_width, expected_height)
            return PROBE_REJECTED
    return PROBE_OK


def _unchanged(film: dict, selection: hero_media.HeroSelection) -> bool:
    return (str(film.get("hero_url") or "") == selection.url
            and film.get("hero_type") == selection.hero_type
            and film.get("hero_source") == selection.source)


async def enrich_film_hero(film: dict, *, session=None, probe_session=None,
                           dry_run: bool = False, verify: bool = True) -> HeroOutcome:
    film_id = int(film["id"])
    title = str(film.get("title") or "")
    # Флаг решает, делает ли систему это САМА. Осмотр (--dry-run) он не запрещает:
    # он ничего не записывает, а посмотреть на качество отбора нужно ДО включения,
    # иначе прапорец пришлось бы включать вслепую.
    if not FANART_HERO_ENABLED and not dry_run:
        return HeroOutcome(film_id, title, ACTION_DISABLED)

    images: list[fanart.FanartImage] = []
    if fanart.normalize_imdb_id(film.get("imdb_id")):
        try:
            images = await fanart.get_movie_backgrounds(film["imdb_id"], session=session)
        except fanart.FanartUnavailable:
            # Временный сбой источника. Сохранённый выбор не трогаем и отметку
            # проверки не ставим — иначе фильм ушёл бы на 30 дней из-за таймаута.
            return HeroOutcome(film_id, title, ACTION_UNAVAILABLE)

    selection = hero_media.choose_hero(fanart_images=images, poster_url=film.get("poster_url"))
    if (selection is not None and verify and not dry_run
            and selection.source == hero_media.SOURCE_FANART):
        verdict = await probe_image(selection.url, expected_width=selection.width,
                                    expected_height=selection.height, session=probe_session)
        if verdict == PROBE_UNKNOWN:
            # До CDN не достучались. Записать сейчас запасной постер значило бы
            # заморозить фильм на недели из-за чужого получасового сбоя — и, что
            # хуже, сделать это молча. Оставляем как есть и вернёмся позже.
            return HeroOutcome(film_id, title, ACTION_UNAVAILABLE, candidates=len(images))
        if verdict == PROBE_REJECTED:
            # Файл получен и негоден — вот это уже про сам файл: уходим в
            # запасной режим, а не показываем человеку битую ссылку.
            selection = hero_media.poster_fallback(film.get("poster_url"))

    if selection is None:
        if not dry_run:
            await db.mark_film_hero_checked(film_id)
        return HeroOutcome(film_id, title, ACTION_NONE, candidates=len(images))

    outcome = HeroOutcome(film_id, title, ACTION_DRY_RUN if dry_run else ACTION_STORED,
                          hero_type=selection.hero_type, hero_source=selection.source,
                          quality_score=selection.quality_score,
                          width=selection.width, height=selection.height,
                          candidates=len(images))
    if dry_run:
        return outcome
    if _unchanged(film, selection):
        # Выбор тот же — обновляем только отметку проверки, чтобы hero_updated_at
        # продолжал означать «когда изображение реально менялось».
        await db.mark_film_hero_checked(film_id)
        return HeroOutcome(**{**outcome.as_dict(), "action": ACTION_UNCHANGED})
    await db.update_film_hero(
        film_id, hero_url=selection.url, hero_type=selection.hero_type,
        hero_source=selection.source, hero_quality_score=selection.quality_score,
        hero_width=selection.width, hero_height=selection.height)
    return outcome


async def refresh_due_heroes(*, limit: int | None = None, concurrency: int | None = None,
                             dry_run: bool = False, films: list[dict] | None = None,
                             verify: bool = True) -> HeroReport:
    """Обойти пачку кандидатов с ограниченным параллелизмом.

    Падение одного фильма не останавливает пачку: внешний источник ненадёжен по
    определению, и один таймаут не повод бросать остальные 19.
    """
    report = HeroReport()
    if not FANART_HERO_ENABLED and not dry_run:
        return report
    batch = films if films is not None else await db.list_films_missing_or_stale_hero(
        limit=limit or HERO_REFRESH_BATCH)
    if not batch:
        return report

    gate = asyncio.Semaphore(max(1, concurrency or HERO_REFRESH_CONCURRENCY))
    session = aiohttp.ClientSession(timeout=_PROBE_TIMEOUT)
    try:
        async def _one(film: dict) -> HeroOutcome:
            async with gate:
                try:
                    return await enrich_film_hero(film, probe_session=session,
                                                  dry_run=dry_run, verify=verify)
                except Exception:
                    logger.warning("hero: фильм %s не обработан", film.get("id"), exc_info=True)
                    return HeroOutcome(int(film["id"]), str(film.get("title") or ""),
                                       ACTION_UNAVAILABLE)

        for outcome in await asyncio.gather(*(_one(film) for film in batch)):
            report.add(outcome)
    finally:
        await session.close()
    return report
