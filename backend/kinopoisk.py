"""Клиент kinopoisk.dev (хост api.poiskkino.dev) — русские названия, постеры,
рейтинги Кинопоиска и IMDb одним источником. Токен: @kinopoiskdev_bot в Telegram.

Ответ поиска уже содержит всё нужное (постер, рейтинги, imdb id, жанры,
описание, длительность), поэтому второй запрос за деталями обычно не нужен.
"""
import logging
from collections import OrderedDict

import aiohttp
import cast as cast_model
import ratelimit
from config import KINOPOISK_TOKENS

logger = logging.getLogger(__name__)

BASE = "https://api.poiskkino.dev/v1.4"
_TIMEOUT = aiohttp.ClientTimeout(total=12)
_session: aiohttp.ClientSession | None = None

# Индекс round-robin по пулу токенов (ключ передаём per-request, не в сессии).
_rr = 0

# Детали по id не меняются — кэшируем (экономим лимит 200 запросов/сутки).
_cache: OrderedDict[str, dict] = OrderedDict()
_CACHE_MAX = 500
_person_cache: OrderedDict[str, dict] = OrderedDict()
_PERSON_CACHE_MAX = 1000


def _cache_put(kp_id: str, doc: dict) -> None:
    """LRU без cache cliff: новый фильм вытесняет один самый старый, а не все 500."""
    key = str(kp_id)
    _cache.pop(key, None)
    _cache[key] = doc
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)

# Поля, которые точно нужны — просим только их (меньше трафика).
_FIELDS = [
    "id", "name", "alternativeName", "year", "type", "isSeries", "description",
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


def extract_actor_photos(persons: list[dict], max_actors: int = 5) -> list[dict]:
    """Те же актёры, что и extract_credits (тот же порядок/лимит) — но с фото,
    под карточки в UI. photo_url может быть None у конкретного актёра (фронтенд
    тогда падает на аватар-заглушку с инициалами)."""
    out = []
    for p in persons or []:
        name = p.get("name")
        if not name or p.get("enProfession") != "actor":
            continue
        out.append({"name": name, "photo_url": p.get("photo"), "source": "kinopoisk"})
        if len(out) >= max_actors:
            break
    return out


def extract_cast(persons: list[dict], *, limit: int = 12) -> list[dict]:
    """Return movie-specific, provider-ordered actors without dropping identity."""
    result: list[dict] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    actor_order = 0
    for person in persons or []:
        if person.get("enProfession") != "actor":
            continue
        name = str(person.get("name") or person.get("enName") or "").strip()
        if not name:
            continue
        person_id = (
            str(person["id"]).strip() if person.get("id") is not None else None
        )
        original_name = str(person.get("enName") or "").strip() or None
        identity_name = cast_model.normalize_person_name(original_name or name)
        if person_id:
            if person_id in seen_ids:
                continue
            seen_ids.add(person_id)
        elif identity_name:
            if identity_name in seen_names:
                continue
            seen_names.add(identity_name)
        photo_url = cast_model.valid_http_url(person.get("photo"))
        result.append({
            "person_id": person_id,
            "wikidata_id": None,
            "name": name,
            "original_name": original_name,
            "character": str(person.get("description") or "").strip() or None,
            "billing_order": actor_order,
            "source": "kinopoisk",
            "photo_url": photo_url,
            "fallback_photo_urls": [],
            "photo_state": (
                cast_model.PHOTO_UNVERIFIED if photo_url
                else cast_model.PHOTO_MISSING
            ),
        })
        actor_order += 1
        if len(result) >= limit:
            break
    return cast_model.normalize_cast(result, limit=limit)


def extract_director_photos(persons: list[dict], max_directors: int = 2) -> list[dict]:
    """Director portraits from the same Kinopoisk payload as film credits.

    They are a strictly bounded fallback for Wikidata/Commons: many excellent
    directors simply do not have a free Commons P18 image, while Kinopoisk
    often has a legitimate press portrait in this already-fetched response.
    """
    out = []
    for p in persons or []:
        name = p.get("name")
        if not name or p.get("enProfession") != "director":
            continue
        out.append({"name": name, "photo_url": p.get("photo"), "source": "kinopoisk"})
        if len(out) >= max_directors:
            break
    return out


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
    last = None
    for i in range(len(keys)):
        idx = (start + i) % len(keys)
        # This is the one budget gate for every real request to Kinopoisk:
        # search, details, artwork and maintenance backfills alike.
        if not await ratelimit.try_spend_search():
            logger.warning("Kinopoisk budget exhausted before %s", path)
            return None
        try:
            session = await _get_session()
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
        except (TimeoutError, aiohttp.ClientError) as e:
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
            _cache_put(str(d["id"]), d)
    return docs


async def get_movie(kp_id: str) -> dict | None:
    """Полные данные по id Кинопоиска (из кэша, если поиск их уже принёс)."""
    cached = _cache.get(str(kp_id))
    if cached is not None:
        _cache.move_to_end(str(kp_id))
        return cached
    if not KINOPOISK_TOKENS:
        return None
    params = [("selectFields", f) for f in _FIELDS]
    data = await _request(f"/movie/{kp_id}", params)
    if data and data.get("id"):
        _cache_put(str(kp_id), data)
    return data


def _person_cache_put(person_id: str, person: dict) -> None:
    key = str(person_id)
    _person_cache.pop(key, None)
    _person_cache[key] = person
    if len(_person_cache) > _PERSON_CACHE_MAX:
        _person_cache.popitem(last=False)


async def get_person(person_id: str) -> dict | None:
    """Bounded, shared-budget portrait fallback for background enrichment only."""
    key = str(person_id or "").strip()
    if not key:
        return None
    cached = _person_cache.get(key)
    if cached is not None:
        _person_cache.move_to_end(key)
        return cached
    params = [
        ("selectFields", "id"),
        ("selectFields", "name"),
        ("selectFields", "enName"),
        ("selectFields", "photo"),
    ]
    data = await _request(f"/person/{key}", params)
    if data and data.get("id") is not None:
        _person_cache_put(key, data)
        return data
    return None


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


async def assets_by_imdb(imdb_ids: list[str]) -> dict[str, dict]:
    """Poster, backdrop and age rating in one Kinopoisk request per batch."""
    ids = [item for item in imdb_ids if item and item.startswith("tt")]
    if not KINOPOISK_TOKENS or not ids:
        return {}
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 40):
        chunk = ids[start:start + 40]
        params = [("externalId.imdb", item) for item in chunk]
        params += [("selectFields", "id"), ("selectFields", "externalId"), ("selectFields", "poster"),
                   ("selectFields", "backdrop"), ("selectFields", "ageRating"),
                   ("limit", "250"), ("page", "1")]
        data = await _request("/movie", params)
        if not data:
            continue
        for movie in data.get("docs", []):
            imdb_id = (movie.get("externalId") or {}).get("imdb")
            if imdb_id:
                out[imdb_id] = {
                    "kp_id": str(movie["id"]) if movie.get("id") is not None else None,
                    "poster_url": (movie.get("poster") or {}).get("url"),
                    "backdrop_url": (movie.get("backdrop") or {}).get("url"),
                    "age_rating": age_rating_of(movie),
                }
    return out


async def actor_photos_by_imdb(imdb_ids: list[str]) -> dict[str, list[dict]]:
    """Фото актёров по списку IMDb ID: {imdb_id: [{name, photo_url}]}. Батчами
    (для бекфила). persons — «тяжёлое» поле, батч мельче, чем у остальных."""
    ids = [i for i in imdb_ids if i and i.startswith("tt")]
    if not KINOPOISK_TOKENS or not ids:
        return {}
    out: dict[str, list[dict]] = {}
    for start in range(0, len(ids), 20):
        chunk = ids[start:start + 20]
        params = [("externalId.imdb", x) for x in chunk]
        params += [("selectFields", "externalId"), ("selectFields", "persons"),
                   ("limit", "250"), ("page", "1")]
        data = await _request("/movie", params)
        if not data:
            continue
        for m in data.get("docs", []):
            imdb = (m.get("externalId") or {}).get("imdb")
            if not imdb:
                continue
            photos = extract_actor_photos(m.get("persons") or [])
            if photos:
                out[imdb] = photos
    return out


async def cast_documents_by_imdb(imdb_ids: list[str]) -> dict[str, dict]:
    """Canonical cast source documents keyed by IMDb ID.

    Older catalog rows created through the OMDb fallback may not have ``kp_id``.
    Resolve those rows through their stable IMDb identity in bounded batches so
    the canonical pipeline still uses Kinopoisk billing order instead of
    silently falling back to Wikidata order.
    """
    ids = list(dict.fromkeys(
        item for item in imdb_ids if item and item.startswith("tt")
    ))
    if not KINOPOISK_TOKENS or not ids:
        return {}
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 20):
        chunk = ids[start:start + 20]
        params = [("externalId.imdb", item) for item in chunk]
        params += [
            ("selectFields", "id"),
            ("selectFields", "externalId"),
            ("selectFields", "persons"),
            ("limit", "250"),
            ("page", "1"),
        ]
        data = await _request("/movie", params)
        if not data:
            continue
        for movie in data.get("docs", []):
            imdb_id = (movie.get("externalId") or {}).get("imdb")
            if imdb_id in chunk and movie.get("id") is not None:
                out[imdb_id] = movie
                _cache_put(str(movie["id"]), movie)
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
