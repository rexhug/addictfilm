"""Клиент Fanart.tv: горизонтальные кадры фильмов по IMDb ID.

Зачем вообще внешний источник. У kinopoisk.dev есть `backdrop_url`, но НАЛИЧИЕ
этого поля ничего не доказывает: там встречается и вертикальная обложка, и
640×360, и кадр с выжженным логотипом. Полноэкранный экран подбора на таком
изображении выглядит как ошибка вёрстки. Fanart отдаёт размеры, язык и число
лайков ДО скачивания файла, поэтому отбор можно сделать по метаданным, не
качая каталог целиком.

Ключи. По документации Fanart project key уходит заголовком `api-key`, личный —
`client-key`; одновременная отправка обоих поддерживается официально. Значения
приходят только из окружения и никогда не попадают ни в URL, ни в лог, ни в
текст исключения: у 401/403 сообщение обязано называть переменную, а не её
содержимое.

Транспорт — aiohttp, как во всём остальном проекте (kinopoisk.py, omdb.py,
прокси картинок). Отдельный httpx сюда добавил бы вторую HTTP-библиотеку в
образ ради одного модуля.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from config import FANART_CLIENT_KEY, FANART_PROJECT_KEY

logger = logging.getLogger(__name__)

_BASE_URL = "https://webservice.fanart.tv/v3.2"
_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)
_MAX_ATTEMPTS = 3
_MAX_RETRY_DELAY = 15.0

# Единственный CDN, с которого Fanart отдаёт файлы. Всё остальное — не наш
# ответ, и пропускать его в прокси картинок нельзя (анти-SSRF).
IMAGE_HOST = "assets.fanart.tv"

_session: aiohttp.ClientSession | None = None


@dataclass(frozen=True)
class FanartImage:
    id: str
    url: str
    language: str
    likes: int
    width: int
    height: int


class FanartUnavailable(RuntimeError):
    """Временная недоступность источника. Отличается от «артворка нет»:
    при ней сохранённый выбор НЕ трогаем и повторяем позже."""


def configured() -> bool:
    return bool(FANART_PROJECT_KEY or FANART_CLIENT_KEY)


def _headers() -> dict[str, str]:
    headers = {"accept": "application/json"}
    if FANART_PROJECT_KEY:
        headers["api-key"] = FANART_PROJECT_KEY
    if FANART_CLIENT_KEY:
        headers["client-key"] = FANART_CLIENT_KEY
    return headers


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_TIMEOUT)   # ключи шлём per-request
    return _session


async def close() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_imdb_id(value: Any) -> str:
    imdb_id = str(value or "").strip().lower()
    # Отсекаем и пустое, и мусор вида "tt" или "ttx": в путь URL должен попадать
    # только заведомо корректный идентификатор.
    if not imdb_id.startswith("tt") or not imdb_id[2:].isdigit():
        return ""
    return imdb_id


def _parse_image(raw: Any) -> FanartImage | None:
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or "").strip()
    width, height = _as_int(raw.get("width")), _as_int(raw.get("height"))
    if not url.startswith(("http://", "https://")) or width <= 0 or height <= 0:
        return None
    return FanartImage(
        id=str(raw.get("id") or ""),
        # Telegram WebView не грузит смешанный контент — http приводим к https.
        url=("https://" + url.split("://", 1)[1]) if url.startswith("http://") else url,
        language=str(raw.get("lang") or "00").strip().lower() or "00",
        likes=max(0, _as_int(raw.get("likes"))),
        width=width,
        height=height,
    )


def _retry_delay(retry_after: str | None) -> float:
    try:
        return min(_MAX_RETRY_DELAY, max(1.0, float(retry_after or 1)))
    except (TypeError, ValueError):
        return 1.0


async def get_movie_backgrounds(imdb_id: str, *, session=None) -> list[FanartImage]:
    """Список горизонтальных кадров. Пустой список — «артворка нет», это норма.

    Исключение FanartUnavailable — только про недоступность источника.
    """
    normalized = normalize_imdb_id(imdb_id)
    if not normalized or not configured():
        return []

    http = session or await _get_session()
    url = f"{_BASE_URL}/movies/{normalized}"

    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with http.get(url, headers=_headers()) as response:
                if response.status == 404:
                    return []
                if response.status == 429:
                    if attempt == _MAX_ATTEMPTS - 1:
                        raise FanartUnavailable("Fanart.tv: лимит запросов")
                    await asyncio.sleep(_retry_delay(response.headers.get("Retry-After")))
                    continue
                if response.status in (401, 403):
                    # Ошибка настройки, а не данных: называем переменные, но не
                    # значения — иначе ключ уедет в лог и в Sentry.
                    raise FanartUnavailable(
                        "Fanart.tv отклонил ключи — проверьте FANART_PROJECT_KEY/FANART_CLIENT_KEY")
                if response.status >= 500:
                    if attempt == _MAX_ATTEMPTS - 1:
                        raise FanartUnavailable(f"Fanart.tv вернул {response.status}")
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except FanartUnavailable:
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            logger.warning("Fanart: запрос по %s не удался (%s)", normalized, type(exc).__name__)
            raise FanartUnavailable("Fanart.tv недоступен") from exc

        if not isinstance(payload, dict):
            return []
        parsed = (_parse_image(item) for item in (payload.get("moviebackground") or []))
        return [image for image in parsed if image is not None]

    return []
