"""Клиент kinopoisk.dev (хост api.poiskkino.dev) — русские названия, постеры,
рейтинги Кинопоиска и IMDb одним источником. Токен: @kinopoiskdev_bot в Telegram.

Ответ поиска уже содержит всё нужное (постер, рейтинги, imdb id, жанры,
описание, длительность), поэтому второй запрос за деталями обычно не нужен.
"""
import asyncio
import logging

import aiohttp

from config import KINOPOISK_TOKENS

logger = logging.getLogger(__name__)

BASE = "https://api.poiskkino.dev/v1.4"
_TIMEOUT = aiohttp.ClientTimeout(total=12)
_session: aiohttp.ClientSession | None = None

# Индекс round-robin по пулу токенов (ключ передаём per-request, не в сессии).
_rr = 0

# Детали по id не меняются — кэшируем (экономим лимит 200 запросов/сутки).
_cache: dict[str, dict] = {}
_CACHE_MAX = 500

# Поля, которые точно нужны — просим только их (меньше трафика).
_FIELDS = [
    "id", "name", "alternativeName", "year", "type", "description",
    "shortDescription", "movieLength", "seriesLength",
    "rating.kp", "rating.imdb", "votes.imdb",
    "poster.url", "poster.previewUrl", "backdrop.url",
    "genres.name", "externalId.imdb", "ageRating",
    "persons",
]


def age_rating_of(doc: dict) -> str | None:
    """«18+» и т.п. из числового ageRating кинопоиска (0/None — рейтинга нет)."""
    n = doc.get("ageRating")
    return f"{n}+" if n else None


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
        _session = aiohttp.ClientSession(timeout=_TIMEOUT)  # ключ шлём per-request
    return _session


async def aclose() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


async def _request(path: str, params) -> dict | None:
    """GET к kinopoisk с ротацией токенов. Перебираем пул по кругу; при
    401/402/403/429 (квота/доступ) пробуем следующий ключ. None = все ключи не
    дали ответа (или пул пуст)."""
    global _rr
    keys = KINOPOISK_TOKENS
    if not keys:
        return None
    start = _rr
    _rr = (_rr + 1) % len(keys)  # следующий вызов стартует со следующего ключа
    session = await _get_session()
    last = None
    for i in range(len(keys)):
        idx = (start + i) % len(keys)
        try:
            async with session.get(f"{BASE}{path}", params=params,
                                   headers={"X-API-KEY": keys[idx]}) as resp:
                if resp.status == 200:
                    return await resp.json()
                last = resp.status
                if resp.status in (401, 402, 403, 429):  # квота/доступ — следующий ключ
                    logger.warning("Kinopoisk %s: ключ #%d → HTTP %s, пробую следующий",
                                   path, idx, resp.status)
                    continue
                logger.warning("Kinopoisk %s -> HTTP %s", path, resp.status)
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("Kinopoisk %s error: %s", path, e)
            last = "err"
            continue
    logger.warning("Kinopoisk %s: пул ключей исчерпан (last=%s)", path, last)
    return None


async def search_movies(query: str, limit: int = 8) -> list[dict]:
    """Поиск по названию. Возвращает список «сырых» документов (или [])."""
    if not KINOPOISK_TOKENS:
        return []
    params = [("query", query), ("limit", str(limit)), ("page", "1")]
    params += [("selectFields", f) for f in _FIELDS]
    data = await _request("/movie/search", params)
    if not data:
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
    if not KINOPOISK_TOKENS:
        return None
    params = [("selectFields", f) for f in _FIELDS]
    data = await _request(f"/movie/{kp_id}", params)
    if data and data.get("id"):
        _cache[str(kp_id)] = data
    return data


async def ratings_by_imdb(imdb_ids: list[str]) -> dict[str, float]:
    """Рейтинги Кинопоиска по списку IMDb ID: {imdb_id: kp}. Батчами (для бекфила)."""
    ids = [i for i in imdb_ids if i and i.startswith("tt")]
    if not KINOPOISK_TOKENS or not ids:
        return {}
    out: dict[str, float] = {}
    for start in range(0, len(ids), 40):
        chunk = ids[start:start + 40]
        params = [("externalId.imdb", x) for x in chunk]
        params += [("selectFields", "externalId"), ("selectFields", "rating"),
                   ("limit", "250"), ("page", "1")]
        data = await _request("/movie", params)
        if not data:
            continue
        for m in data.get("docs", []):
            imdb = (m.get("externalId") or {}).get("imdb")
            kp = (m.get("rating") or {}).get("kp")
            if imdb and kp:
                out[imdb] = kp
    return out


async def posters_by_imdb(imdb_ids: list[str]) -> dict[str, str]:
    """Постеры Кинопоиска по списку IMDb ID: {imdb_id: poster_url}. Батчами (для бекфила).
    Возвращает только реально найденные постеры (без previewUrl-заглушек)."""
    ids = [i for i in imdb_ids if i and i.startswith("tt")]
    if not KINOPOISK_TOKENS or not ids:
        return {}
    out: dict[str, str] = {}
    for start in range(0, len(ids), 40):
        chunk = ids[start:start + 40]
        params = [("externalId.imdb", x) for x in chunk]
        params += [("selectFields", "externalId"), ("selectFields", "poster"),
                   ("limit", "250"), ("page", "1")]
        data = await _request("/movie", params)
        if not data:
            continue
        for m in data.get("docs", []):
            imdb = (m.get("externalId") or {}).get("imdb")
            url = (m.get("poster") or {}).get("url")
            if imdb and url:
                out[imdb] = url
    return out


async def artwork_by_imdb(imdb_ids: list[str]) -> dict[str, dict]:
    """Backdrop + возрастной рейтинг по списку IMDb ID: {imdb_id: {backdrop_url, age_rating}}.
    Батчами (для бекфила уже добавленных фильмов)."""
    ids = [i for i in imdb_ids if i and i.startswith("tt")]
    if not KINOPOISK_TOKENS or not ids:
        return {}
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 40):
        chunk = ids[start:start + 40]
        params = [("externalId.imdb", x) for x in chunk]
        params += [("selectFields", "externalId"), ("selectFields", "backdrop"),
                   ("selectFields", "ageRating"), ("limit", "250"), ("page", "1")]
        data = await _request("/movie", params)
        if not data:
            continue
        for m in data.get("docs", []):
            imdb = (m.get("externalId") or {}).get("imdb")
            if not imdb:
                continue
            out[imdb] = {
                "backdrop_url": (m.get("backdrop") or {}).get("url"),
                "age_rating": age_rating_of(m),
            }
    return out


async def credits_by_imdb(imdb_ids: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """(режиссёры, актёры) по списку IMDb ID: {imdb_id: (directors, actors)}. Для бекфила."""
    ids = [i for i in imdb_ids if i and i.startswith("tt")]
    if not KINOPOISK_TOKENS or not ids:
        return {}
    out: dict[str, tuple[str | None, str | None]] = {}
    for start in range(0, len(ids), 30):
        chunk = ids[start:start + 30]
        params = [("externalId.imdb", x) for x in chunk]
        params += [("selectFields", "externalId"), ("selectFields", "persons"),
                   ("limit", "250"), ("page", "1")]
        data = await _request("/movie", params)
        if not data:
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
