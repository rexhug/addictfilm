"""Клиент kinopoisk.dev (хост api.poiskkino.dev) — русские названия, постеры,
рейтинги Кинопоиска и IMDb одним источником. Токен: @kinopoiskdev_bot в Telegram.

Ответ поиска уже содержит всё нужное (постер, рейтинги, imdb id, жанры,
описание, длительность), поэтому второй запрос за деталями обычно не нужен.
"""
import asyncio
import logging

import aiohttp

from config import KINOPOISK_TOKEN

logger = logging.getLogger(__name__)

BASE = "https://api.poiskkino.dev/v1.4"
_TIMEOUT = aiohttp.ClientTimeout(total=12)
_session: aiohttp.ClientSession | None = None

# Детали по id не меняются — кэшируем (экономим лимит 200 запросов/сутки).
_cache: dict[str, dict] = {}
_CACHE_MAX = 500

# Поля, которые точно нужны — просим только их (меньше трафика).
_FIELDS = [
    "id", "name", "alternativeName", "year", "type", "description",
    "shortDescription", "movieLength", "seriesLength",
    "rating.kp", "rating.imdb", "votes.imdb",
    "poster.url", "poster.previewUrl", "genres.name", "externalId.imdb",
    "persons",
]


def extract_credits(persons: list[dict], max_actors: int = 5) -> tuple[str | None, str | None]:
    """Из списка persons -> (режиссёры, актёры) строками через запятую (рус. имена)."""
    directors, actors = [], []
    for p in persons or []:
        name = p.get("name")
        if not name:
            continue
        prof = p.get("enProfession")
        if prof == "director" and len(directors) < 2:
            directors.append(name)
        elif prof == "actor" and len(actors) < max_actors:
            actors.append(name)
    return (", ".join(directors) or None), (", ".join(actors) or None)


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=_TIMEOUT, headers={"X-API-KEY": KINOPOISK_TOKEN},
        )
    return _session


async def aclose() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


async def search_movies(query: str, limit: int = 8) -> list[dict]:
    """Поиск по названию. Возвращает список «сырых» документов (или [])."""
    if not KINOPOISK_TOKEN:
        return []
    session = await _get_session()
    params = [("query", query), ("limit", str(limit)), ("page", "1")]
    params += [("selectFields", f) for f in _FIELDS]
    try:
        async with session.get(f"{BASE}/movie/search", params=params) as resp:
            if resp.status != 200:
                logger.warning("Kinopoisk search %r -> HTTP %s", query, resp.status)
                return []
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("Kinopoisk search error: %s", e)
        return []
    docs = data.get("docs", [])
    for d in docs:
        if d.get("id"):
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
            _cache[str(d["id"])] = d
    return docs


async def get_movie(kp_id: str) -> dict | None:
    """Полные данные по id Кинопоиска (из кэша, если поиск их уже принёс)."""
    cached = _cache.get(str(kp_id))
    if cached is not None:
        return cached
    if not KINOPOISK_TOKEN:
        return None
    session = await _get_session()
    params = [("selectFields", f) for f in _FIELDS]
    try:
        async with session.get(f"{BASE}/movie/{kp_id}", params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("Kinopoisk get_movie %s error: %s", kp_id, e)
        return None
    if data.get("id"):
        _cache[str(kp_id)] = data
    return data


async def ratings_by_imdb(imdb_ids: list[str]) -> dict[str, float]:
    """Рейтинги Кинопоиска по списку IMDb ID: {imdb_id: kp}. Батчами (для бекфила)."""
    ids = [i for i in imdb_ids if i and i.startswith("tt")]
    if not KINOPOISK_TOKEN or not ids:
        return {}
    session = await _get_session()
    out: dict[str, float] = {}
    for start in range(0, len(ids), 40):
        chunk = ids[start:start + 40]
        params = [("externalId.imdb", x) for x in chunk]
        params += [("selectFields", "externalId"), ("selectFields", "rating"),
                   ("limit", "250"), ("page", "1")]
        try:
            async with session.get(f"{BASE}/movie", params=params) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("Kinopoisk ratings_by_imdb error: %s", e)
            continue
        for m in data.get("docs", []):
            imdb = (m.get("externalId") or {}).get("imdb")
            kp = (m.get("rating") or {}).get("kp")
            if imdb and kp:
                out[imdb] = kp
    return out


async def credits_by_imdb(imdb_ids: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """(режиссёры, актёры) по списку IMDb ID: {imdb_id: (directors, actors)}. Для бекфила."""
    ids = [i for i in imdb_ids if i and i.startswith("tt")]
    if not KINOPOISK_TOKEN or not ids:
        return {}
    session = await _get_session()
    out: dict[str, tuple[str | None, str | None]] = {}
    for start in range(0, len(ids), 30):
        chunk = ids[start:start + 30]
        params = [("externalId.imdb", x) for x in chunk]
        params += [("selectFields", "externalId"), ("selectFields", "persons"),
                   ("limit", "250"), ("page", "1")]
        try:
            async with session.get(f"{BASE}/movie", params=params) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("Kinopoisk credits_by_imdb error: %s", e)
            continue
        for m in data.get("docs", []):
            imdb = (m.get("externalId") or {}).get("imdb")
            if imdb:
                out[imdb] = extract_credits(m.get("persons") or [])
    return out


_SERIES_TYPES = {"tv-series", "animated-series", "anime"}


def is_series(doc: dict) -> bool:
    return doc.get("type") in _SERIES_TYPES


def imdb_id_of(doc: dict) -> str:
    """IMDb ID (для дедупликации), либо синтетический kp_<id>."""
    ext = (doc.get("externalId") or {}).get("imdb")
    return ext or f"kp_{doc.get('id')}"
