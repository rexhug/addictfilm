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
import time
from dataclasses import dataclass, field

import aiohttp
import database as db
import fanart
import hero_media
from config import (
    FANART_HERO_ENABLED,
    HERO_REFRESH_BATCH,
    HERO_REFRESH_CONCURRENCY,
    KINOPOISK_HERO_ENABLED,
)

logger = logging.getLogger(__name__)

# Проверка выбранного файла нужна ровно чтобы поймать «метаданные врут»: битую
# ссылку, html-заглушку вместо картинки, обрезанный файл. Скачивать ради
# скоринга весь каталог не нужно — размеры приходят от API.
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=12, connect=5)
_PROBE_MAX_BYTES = 12 * 1024 * 1024
_PROBE_MIN_BYTES = 2048
# Расхождение с метаданными в пределах округления допустимо, кратное — нет.
_PROBE_SIZE_TOLERANCE = 0.05
# 64 КиБ, а не 4: у JPEG сегмент SOF идёт ПОСЛЕ EXIF и цветового профиля, и в
# первые 4 КиБ он помещается далеко не всегда. Для kinopoisk это критично —
# нераспознанный размер там означает «кадр не доказан», то есть потерю годного
# изображения на ровном месте.
_PROBE_HEAD_BYTES = 64 * 1024

PROBE_OK = "ok"
PROBE_REJECTED = "rejected"
PROBE_UNKNOWN = "unknown"

# Классификация ответа CDN. Разделение проходит не по «успех/не успех», а по
# вопросу «это про ФАЙЛ или про то, что мы сейчас не смогли его получить».
# 401/403 попали в временные намеренно: у CDN картинок нет нашей авторизации,
# и такой ответ от него означает защиту от нагрузки или сбой раздачи, а не
# «этого изображения не существует». Понизить фильм навсегда из-за него нельзя.
_TRANSIENT_HTTP_STATUSES = frozenset({401, 403, 408, 425, 429})
# Ответы, которые ДОКАЗЫВАЮТ, что файла нет.
_PERMANENT_HTTP_STATUSES = frozenset({404, 410})


def _probe_status_verdict(status: int) -> str:
    if status == 200:
        return PROBE_OK
    if status in _PERMANENT_HTTP_STATUSES:
        return PROBE_REJECTED
    if status in _TRANSIENT_HTTP_STATUSES or 500 <= status <= 599:
        return PROBE_UNKNOWN
    return PROBE_REJECTED

# Предохранитель. Глобальный сбой Fanart выглядел так: воркер каждые 15 минут
# честно делал двадцать одинаково безрезультатных запросов. Толку от них нет ни
# нам, ни чужому сервису, который в этот момент и так лежит.
#
# Состояние процессное, а не в БД, и это осознанно: оно про «эта машина только
# что видела сбой», живёт минуты и обязано исчезать при рестарте. Класть такое
# в общую базу значило бы дать одной машине заглушить остальные.
_CIRCUIT_UNKNOWN_LIMIT = 3
_CIRCUIT_COOLDOWN_SECONDS = 60 * 60
_circuit_open_until = 0.0


def _circuit_is_open() -> bool:
    return time.monotonic() < _circuit_open_until


def _open_circuit() -> None:
    global _circuit_open_until
    _circuit_open_until = time.monotonic() + _CIRCUIT_COOLDOWN_SECONDS


def reset_circuit() -> None:
    """Сбросить предохранитель. Нужен тестам, чтобы они не зависели друг от
    друга, и оператору — когда он знает, что чужой сбой уже кончился."""
    global _circuit_open_until
    _circuit_open_until = 0.0


ACTION_STORED = "stored"
ACTION_UNCHANGED = "unchanged"
ACTION_NONE = "none"
ACTION_UNAVAILABLE = "unavailable"
ACTION_DISABLED = "disabled"
ACTION_DRY_RUN = "dry_run"


@dataclass(frozen=True)
class ImageProbeResult:
    verdict: str
    width: int | None = None
    height: int | None = None


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
    # Отдельно от action: фильм может УСПЕШНО получить кадр от kinopoisk в тот
    # самый момент, когда Fanart лежит. Если считать сбой Fanart по общему
    # результату, предохранитель никогда не сработает и воркер продолжит
    # долбить упавший сервис.
    fanart_unavailable: bool = False

    def as_dict(self) -> dict:
        return {"film_id": self.film_id, "title": self.title, "action": self.action,
                "hero_type": self.hero_type, "hero_source": self.hero_source,
                "quality_score": self.quality_score, "width": self.width,
                "height": self.height, "candidates": self.candidates,
                "fanart_unavailable": self.fanart_unavailable}


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


async def probe_image_info(url: str, *, expected_width: int | None = None,
                           expected_height: int | None = None, session=None) -> ImageProbeResult:
    """Проверка файла с ВОЗВРАТОМ настоящих размеров.

    Три исхода, а не два. Различие принципиальное, и оно проявилось сразу на
    проде: CDN Fanart лежал, проверка не проходила — и «не смог проверить»
    трактовалось как «файл плохой». В итоге весь каталог уезжал в запасной
    постер И получал свежую отметку проверки, то есть замораживался на недели
    из-за чужого получасового сбоя.

    PROBE_OK        файл получен, распознан и не противоречит метаданным;
    PROBE_REJECTED  файл получен и негоден (не картинка, обрезан, врут размеры),
                    либо источник доказал, что файла нет (404/410);
    PROBE_UNKNOWN   до файла не достучались — сеть, DNS, TLS, таймаут, 5xx,
                    429/408/425 и даже 401/403: это про наш доступ, не про файл.

    Размеры нужны наружу ради kinopoisk: там нет метаданных вообще, и
    единственное доказательство пригодности кадра — заголовок самого файла.
    """
    owns = session is None
    http = session or aiohttp.ClientSession(timeout=_PROBE_TIMEOUT)
    try:
        async with http.get(url) as response:
            status_verdict = _probe_status_verdict(response.status)
            if status_verdict != PROBE_OK:
                return ImageProbeResult(status_verdict)
            # Дальше — только про содержимое ответа 200.
            if not str(response.headers.get("Content-Type", "")).lower().startswith("image/"):
                return ImageProbeResult(PROBE_REJECTED)
            head = b""
            size = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > _PROBE_MAX_BYTES:
                    return ImageProbeResult(PROBE_REJECTED)
                if len(head) < _PROBE_HEAD_BYTES:
                    head += chunk
    except (TimeoutError, aiohttp.ClientError) as exc:
        logger.info("hero: файл не удалось проверить (%s)", type(exc).__name__)
        return ImageProbeResult(PROBE_UNKNOWN)
    finally:
        if owns:
            await http.close()

    if size < _PROBE_MIN_BYTES:
        return ImageProbeResult(PROBE_REJECTED)
    sniffed = _sniff_dimensions(head)
    width, height = sniffed if sniffed else (None, None)
    if sniffed and expected_width and expected_height:
        if (abs(width - expected_width) / expected_width > _PROBE_SIZE_TOLERANCE
                or abs(height - expected_height) / expected_height > _PROBE_SIZE_TOLERANCE):
            logger.warning("hero: файл не совпал с метаданными источника (%sx%s против %sx%s)",
                           width, height, expected_width, expected_height)
            return ImageProbeResult(PROBE_REJECTED, width, height)
    return ImageProbeResult(PROBE_OK, width, height)


async def probe_image(url: str, *, expected_width: int | None = None,
                      expected_height: int | None = None, session=None) -> str:
    """Совместимая обёртка: только вердикт, без размеров."""
    result = await probe_image_info(url, expected_width=expected_width,
                                    expected_height=expected_height, session=session)
    return result.verdict


def _unchanged(film: dict, selection: hero_media.HeroSelection) -> bool:
    return (str(film.get("hero_url") or "") == selection.url
            and film.get("hero_type") == selection.hero_type
            and film.get("hero_source") == selection.source)


async def _try_kinopoisk(film: dict, *, probe_session) -> tuple[hero_media.HeroSelection | None, bool]:
    """(выбор, был ли временный сбой).

    Ходит только за файлом, ссылка на который УЖЕ лежит в каталоге: никакого
    стороннего API здесь нет. Поэтому канал продолжает работать, когда Fanart
    недоступен, — ради этого он и заведён.
    """
    # Сохранённый размер в ссылке — это то, что вернул API, а не единственная
    # доступная редакция файла: тот же CDN по тому же пути отдаёт крупнее.
    # Пробуем предпочтительный размер, затем исходный. Решает всё равно
    # заголовок скачанного файла, а не наши ожидания от URL.
    for candidate in hero_media.kinopoisk_rendition_candidates(film.get("backdrop_url")):
        result = await probe_image_info(candidate, session=probe_session)
        if result.verdict == PROBE_UNKNOWN:
            # Сеть, а не файл: второй размер просить бессмысленно, вернёмся позже.
            return None, True
        if result.verdict == PROBE_REJECTED:
            continue      # этой редакции нет — пробуем следующую
        # Размеры взяты из заголовка файла. Их отсутствие — не «наверное
        # подойдёт», а «не доказано»: такой кадр в полноэкранный блок не попадает.
        selection = hero_media.choose_kinopoisk_background(
            candidate, width=result.width, height=result.height)
        if selection is not None:
            return selection, False
    return None, False


async def _try_fanart(film: dict, *, session, probe_session,
                      verify: bool) -> tuple[hero_media.HeroSelection | None, bool, int]:
    """(выбор, был ли временный сбой, сколько кандидатов вернуло API)."""
    if not fanart.normalize_imdb_id(film.get("imdb_id")):
        return None, False, 0
    try:
        images = await fanart.get_movie_backgrounds(film["imdb_id"], session=session)
    except fanart.FanartUnavailable:
        return None, True, 0
    selection = hero_media.choose_fanart_background(images)
    if selection is None or not verify:
        return selection, False, len(images)
    result = await probe_image_info(selection.url, expected_width=selection.width,
                                    expected_height=selection.height, session=probe_session)
    if result.verdict == PROBE_UNKNOWN:
        return None, True, len(images)
    if result.verdict == PROBE_REJECTED:
        return None, False, len(images)
    return selection, False, len(images)


SOURCE_ALL = "all"


def resolve_sources(dry_run: bool, requested: str | None = None) -> tuple[bool, bool]:
    """(проверять kinopoisk, проверять fanart).

    Осмотр обязан показывать то, что сделает ПРОД, а не то, что теоретически
    возможно: иначе он врёт ровно в тот момент, когда по нему принимают решение.
    Поэтому dry-run идёт по включённым источникам. Исключение одно — когда не
    включён ни один: это осмотр «до первого включения», и смотреть тогда нужно
    на всё. Явный --source перекрывает и то, и другое.
    """
    if requested and requested != SOURCE_ALL:
        return requested == hero_media.SOURCE_KINOPOISK, requested == hero_media.SOURCE_FANART
    if requested == SOURCE_ALL:
        return True, True
    inspect_everything = dry_run and not (FANART_HERO_ENABLED or KINOPOISK_HERO_ENABLED)
    return (KINOPOISK_HERO_ENABLED or inspect_everything,
            FANART_HERO_ENABLED or inspect_everything)


async def enrich_film_hero(film: dict, *, session=None, probe_session=None,
                           dry_run: bool = False, verify: bool = True,
                           sources: str | None = None) -> HeroOutcome:
    """Порядок источников: проверенный kinopoisk → проверенный Fanart → постер.

    Kinopoisk идёт первым не из-за качества, а из-за независимости: его ссылка
    уже в каталоге, и она не перестаёт работать, когда чужой сервис лежит.
    Годный кадр от kinopoisk полностью избавляет от запроса к Fanart.
    """
    film_id = int(film["id"])
    title = str(film.get("title") or "")
    # Флаг решает, делает ли систему это САМА. Осмотр (--dry-run) он не запрещает:
    # он ничего не записывает, а посмотреть на качество отбора нужно ДО включения,
    # иначе флаг пришлось бы включать вслепую.
    if not (FANART_HERO_ENABLED or KINOPOISK_HERO_ENABLED) and not dry_run:
        return HeroOutcome(film_id, title, ACTION_DISABLED)

    use_kinopoisk, use_fanart = resolve_sources(dry_run, sources)
    selection: hero_media.HeroSelection | None = None
    kinopoisk_unavailable = False
    fanart_unavailable = False
    candidates = 0

    if use_kinopoisk:
        selection, kinopoisk_unavailable = await _try_kinopoisk(film, probe_session=probe_session)

    # Предохранитель гасит ТОЛЬКО Fanart: проверка kinopoisk выше от него не
    # зависит и продолжает работать во время чужого сбоя.
    if selection is None and use_fanart and not _circuit_is_open():
        selection, fanart_unavailable, candidates = await _try_fanart(
            film, session=session, probe_session=probe_session, verify=verify)

    if selection is None:
        if kinopoisk_unavailable or fanart_unavailable:
            # Хотя бы один источник не ответил, и ни один не дал определённого
            # результата. Записать сейчас запасной постер значило бы заморозить
            # фильм на недели из-за чужого получасового сбоя — и молча.
            return HeroOutcome(film_id, title, ACTION_UNAVAILABLE, candidates=candidates,
                               fanart_unavailable=fanart_unavailable)
        selection = hero_media.poster_fallback(film.get("poster_url"))

    if selection is None:
        if not dry_run:
            await db.mark_film_hero_checked(film_id)
        return HeroOutcome(film_id, title, ACTION_NONE, candidates=candidates,
                           fanart_unavailable=fanart_unavailable)

    outcome = HeroOutcome(film_id, title, ACTION_DRY_RUN if dry_run else ACTION_STORED,
                          hero_type=selection.hero_type, hero_source=selection.source,
                          quality_score=selection.quality_score,
                          width=selection.width, height=selection.height,
                          candidates=candidates, fanart_unavailable=fanart_unavailable)
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
                             verify: bool = True, sources: str | None = None) -> HeroReport:
    """Обойти пачку кандидатов с ограниченным параллелизмом.

    Падение одного фильма не останавливает пачку: внешний источник ненадёжен по
    определению, и один таймаут не повод бросать остальные 19.
    """
    report = HeroReport()
    if not (FANART_HERO_ENABLED or KINOPOISK_HERO_ENABLED) and not dry_run:
        return report
    # Пачку из-за предохранителя больше НЕ пропускаем: он гасит только Fanart,
    # а проверка сохранённых backdrop_url от kinopoisk во время чужого сбоя
    # обязана продолжаться — ради этого второй канал и заведён.
    if _circuit_is_open():
        logger.info("hero: предохранитель Fanart разомкнут, работаем только по kinopoisk")
    batch = films if films is not None else await db.list_films_missing_or_stale_hero(
        limit=limit or HERO_REFRESH_BATCH)
    if not batch:
        return report

    wave_size = max(1, concurrency or HERO_REFRESH_CONCURRENCY)
    session = aiohttp.ClientSession(timeout=_PROBE_TIMEOUT)
    consecutive_unknown = 0
    try:
        async def _one(film: dict) -> HeroOutcome:
            try:
                return await enrich_film_hero(film, probe_session=session,
                                              dry_run=dry_run, verify=verify,
                                              sources=sources)
            except Exception:
                logger.warning("hero: фильм %s не обработан", film.get("id"), exc_info=True)
                return HeroOutcome(int(film["id"]), str(film.get("title") or ""),
                                   ACTION_UNAVAILABLE)

        # Волнами, а не одним gather на всю пачку: предохранитель обязан уметь
        # ПЕРЕСТАТЬ ставить новые запросы. Уже запущенные волну доигрывают —
        # обрывать их незачем, они всё равно ничего не пишут при недоступности.
        for start in range(0, len(batch), wave_size):
            wave = batch[start:start + wave_size]
            for outcome in await asyncio.gather(*(_one(film) for film in wave)):
                report.add(outcome)
                # Считаем сбои ИМЕННО Fanart, а не общий результат: фильм мог
                # успешно получить кадр от kinopoisk ровно в тот момент, когда
                # Fanart лежит, и такой успех не должен прятать чужой сбой.
                if outcome.fanart_unavailable:
                    consecutive_unknown += 1
                elif outcome.action != ACTION_UNAVAILABLE:
                    # Определённый ответ означает, что Fanart жив.
                    consecutive_unknown = 0
            if consecutive_unknown >= _CIRCUIT_UNKNOWN_LIMIT:
                _open_circuit()
                logger.warning(
                    "hero: предохранитель Fanart разомкнут после %s подряд недоступных ответов; "
                    "остаток пачки (%s фильмов) не запрашивается",
                    consecutive_unknown, len(batch) - start - len(wave))
                break
    finally:
        await session.close()
    return report
