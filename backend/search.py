"""Фасад поиска (перенесён из movie_bot, адаптирован под JSON API).

Порядок источников:
  1. kinopoisk.dev — русские названия, постеры, рейтинги КП+IMDb одним запросом.
  2. OMDb + Wikidata — fallback: англ. поиск + официальные рус. названия по SPARQL.

Нормализованный item:
  {src: "k"|"i", ref, title, year, poster, rating, genres, type}
"""
import asyncio
import json
import logging
import math
import os
import re
import time
import unicodedata
from datetime import UTC, datetime

import cast as cast_model
import database as db
import db_runtime
import film_localization as flang
import kinopoisk
import media_type
import omdb
import posters
import ratelimit
import wikidata
from config import KINOPOISK_TOKEN

logger = logging.getLogger(__name__)

# Двухуровневый кэш поиска по нормализованному запросу — одинаковые/популярные
# запросы (в т.ч. от разных пользователей) не тратят лимит источника.
#   L1: in-memory (быстро, но гибнет при рестарте).
#   L2: таблица search_cache в БД (постоянный, общий, переживает деплой).
_QCACHE: dict[str, tuple[float, list]] = {}
_QTTL = 6 * 3600   # свежесть L1, сек
_QMAX = 300        # максимум запросов в L1
# TTL постоянного кэша (БД): результаты поиска стабильны неделями. Настраивается.
_DB_TTL = int(os.getenv("SEARCH_CACHE_TTL_SEC", str(180 * 24 * 3600)))
_EMPTY_DB_TTL = int(os.getenv("SEARCH_EMPTY_CACHE_TTL_SEC", str(6 * 3600)))
SEARCH_CACHE_VERSION = "v4"
_INFLIGHT: dict[str, asyncio.Task] = {}
_puts = 0  # счётчик записей в L2 — для периодической уборки протухшего


def _qnorm(query: str) -> str:
    text = unicodedata.normalize("NFKC", str(query or "")).casefold().replace("ё", "е")
    text = "".join(" " if (unicodedata.category(ch).startswith("P") or ch in "-_—–") else ch for ch in text)
    return " ".join(text.split())


def _query_parts(query: str) -> tuple[str, str | None]:
    normalized = _qnorm(query)
    parts = normalized.rsplit(" ", 1)
    if len(parts) == 2 and re.fullmatch(r"(?:19|20)\d{2}", parts[1]):
        return parts[0], parts[1]
    return normalized, None


def _parse_number(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip().replace(" ", "").replace(",", "")
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        return 0.0


def _title_match(item: dict, query: str) -> tuple[int, int]:
    title_query, year = _query_parts(query)
    tokens = title_query.split()
    title_values = [_qnorm(item.get("title")), _qnorm(item.get("title_original"))]
    title_values = [value for value in title_values if value]
    title_match = 6
    for title in title_values:
        title_tokens = title.split()
        if title == title_query:
            title_match = min(title_match, 0)
        elif title.startswith(title_query + " "):
            title_match = min(title_match, 2)
        elif tokens and all(token in title_tokens for token in tokens):
            title_match = min(title_match, 3)
        elif title_query and title_query in title:
            title_match = min(title_match, 4)
    if title_match == 6:
        metadata = _qnorm(" ".join((item.get("actors") or "", item.get("directors") or "")))
        if title_query and all(token in metadata for token in tokens):
            title_match = 5
    year_match = 1 if year and str(item.get("year") or "")[:4] == year else 0
    return title_match, year_match


def _search_score(item: dict, query: str) -> float:
    match, year_match = _title_match(item, query)
    title_bonus = {0: 100.0, 2: 78.0, 3: 58.0, 4: 38.0, 5: 8.0}.get(match, float("-inf"))
    if not math.isfinite(title_bonus):
        return float("-inf")
    votes = max(_parse_number(item.get("votes")), _parse_number(item.get("imdb_votes")), _parse_number(item.get("kp_votes")))
    rating = max(_parse_number(item.get("rating")), _parse_number(item.get("imdb_rating")), _parse_number(item.get("kp_rating")))
    poster = 1 if item.get("poster") or item.get("poster_url") else 0
    score = title_bonus
    if year_match:
        score += 35.0
    elif _query_parts(query)[1]:
        score -= 20.0
    score += 12.0 * math.log10(votes + 1.0)
    score += 2.5 * rating
    score += 3.0 * poster
    if item.get("type") == "movie":
        score += 2.0
    year_text = str(item.get("year") or "")[:4]
    if year_text.isdigit():
        current = datetime.now(UTC)
        cutoff_year = current.year + ((current.month - 1 + 18) // 12)
        if int(year_text) > cutoff_year:
            score -= 6.0
    return score


def _credible_items(items: list[dict], query: str) -> list[dict]:
    return [item for item in items if _title_match(item, query)[0] <= 5 and math.isfinite(_search_score(item, query))]


def _rank_items(items: list[dict], query: str, limit: int = 8) -> list[dict]:
    candidates = _credible_items(items, query)
    if not candidates:
        return []
    candidates.sort(key=lambda item: (
        -_search_score(item, query),
        -max(_parse_number(item.get("votes")), _parse_number(item.get("imdb_votes")), _parse_number(item.get("kp_votes"))),
        -max(_parse_number(item.get("rating")), _parse_number(item.get("imdb_rating")), _parse_number(item.get("kp_rating"))),
        _qnorm(item.get("title") or item.get("title_original") or ""),
        str(item.get("ref") or item.get("imdb_id") or item.get("kp_id") or ""),
    ))
    if len(candidates) >= 5:
        best = _search_score(candidates[0], query)
        floor = best - max(45.0, abs(best) * 0.45)
        candidates = [item for item in candidates if _search_score(item, query) >= floor]
    return candidates[:limit]


def _strong_title_match(item: dict, query: str) -> bool:
    # Only an exact normalized title is safe to use as a local early return.
    # Franchise prefixes must still be enriched by the providers so that the
    # canonical standalone title is not hidden by a cached sequel.
    return _title_match(item, query)[0] == 0


def _dedup_key(item: dict) -> tuple:
    imdb = str(item.get("imdb_id") or (item.get("ref") if item.get("src") == "i" else "")).lower()
    kp = str(item.get("kp_id") or (item.get("ref") if item.get("src") == "k" else ""))
    if imdb.startswith("tt"):
        return ("imdb", imdb)
    if kp:
        return ("kp", kp)
    return ("title", _qnorm(item.get("title") or item.get("title_original") or ""), str(item.get("year") or "")[:4])


def _merge_items(*groups: list[dict]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for group in groups:
        for item in group:
            key = _dedup_key(item)
            current = merged.get(key)
            if current is None:
                merged[key] = dict(item)
                continue
            for name, value in item.items():
                if value not in (None, "", "N/A") and current.get(name) in (None, "", "N/A"):
                    current[name] = value
    return list(merged.values())


def extract_imdb_id(text: str) -> str | None:
    m = re.search(r"(?<!\w)(tt\d{5,12})(?!\d)", text, flags=re.IGNORECASE)
    return m.group(1).lower() if m else None


def has_cyrillic(text: str) -> bool:
    return bool(text) and bool(re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", text))


def best_title(wikidata_title: str | None, fallback: str) -> str:
    """Название из Wikidata — только если кириллицей (латиница не понижает хорошее)."""
    return wikidata_title if (wikidata_title and has_cyrillic(wikidata_title)) else fallback


def _kp_item(doc: dict) -> dict:
    poster = (doc.get("poster") or {}).get("url")
    r = doc.get("rating") or {}
    v = doc.get("votes") or {}
    rating = r.get("imdb") or r.get("kp")
    title = doc.get("name") or doc.get("alternativeName") or ""
    alternate = doc.get("alternativeName")
    return {
        "src": "k",
        "ref": str(doc["id"]),
        "kp_id": str(doc["id"]),
        "imdb_id": (doc.get("externalId") or {}).get("imdb"),
        "title": title,
        "title_original": alternate if alternate and alternate != title else None,
        "year": str(doc.get("year") or "?"),
        "poster": poster,
        "rating": f"{rating:.1f}" if rating else None,
        "votes": max(_parse_number(v.get("imdb")), _parse_number(v.get("kp"))),
        "genres": ", ".join(g["name"] for g in (doc.get("genres") or [])) or None,
        "type": "series" if kinopoisk.is_series(doc) else "movie",
    }


def _kp_details(doc: dict) -> dict:
    """Kinopoisk response -> permanent catalog record without another request."""
    r = doc.get("rating") or {}
    v = doc.get("votes") or {}
    name = doc.get("name") or doc.get("alternativeName") or ""
    original = doc.get("alternativeName")
    length = doc.get("movieLength") or doc.get("seriesLength")
    directors, _ = kinopoisk.extract_credits(doc.get("persons") or [])
    # enName уже лежит в том же persons — второго запроса за английскими
    # именами режиссёров не делаем.
    directors_en = ", ".join(
        person["enName"] for person in (doc.get("persons") or [])
        if person.get("enProfession") == "director" and person.get("enName")
    ) or None
    kp_genres = ", ".join(g["name"] for g in (doc.get("genres") or [])) or None
    canonical_cast = kinopoisk.extract_cast(doc.get("persons") or [], limit=12)
    actors = ", ".join(member["name"] for member in canonical_cast[:8]) or None
    photos = cast_model.legacy_actor_photos(canonical_cast)
    director_photos = kinopoisk.extract_director_photos(doc.get("persons") or [])
    return {
        "imdb_id": kinopoisk.imdb_id_of(doc),
        "kp_id": str(doc["id"]),
        "title": name,
        "title_original": original if original and original != name else None,
        "year": str(doc["year"]) if doc.get("year") else None,
        "genres": kp_genres,
        "directors": directors,
        "actors": actors,
        "runtime": f"{length} мин" if length else None,
        # Kinopoisk — русскоязычный источник, поэтому его данные идут В РУССКИЕ
        # слоты и никогда в английские. Английским остаётся только оригинальное
        # название и enName, которые провайдер отдаёт как есть.
        # `name` уже упал на alternativeName, если русского названия у записи
        # нет вовсе (kp 459902: name = null). Для общей колонки title это
        # правильный фолбэк, но объявлять латиницу РУССКИМ названием — нет.
        # Запись всё равно отбивает проверка на входе каталога; врать на уровне
        # источника незачем.
        "title_ru": name if flang.looks_russian(name) else None,
        "title_en": original,
        "plot_ru": doc.get("description") or doc.get("shortDescription"),
        "genres_ru": flang.localized_genres(kp_genres, "ru") or None,
        "genres_en": flang.localized_genres(kp_genres, "en") or None,
        "directors_ru": directors,
        "directors_en": directors_en,
        "imdb_rating": f"{r['imdb']:.1f}" if r.get("imdb") else None,
        "kp_rating": f"{r['kp']:.1f}" if r.get("kp") else None,
        "imdb_votes": str(v["imdb"]) if v.get("imdb") else None,
        "plot": doc.get("description") or doc.get("shortDescription"),
        "poster_url": (doc.get("poster") or {}).get("url"),
        "backdrop_url": (doc.get("backdrop") or {}).get("url"),
        "age_rating": kinopoisk.age_rating_of(doc),
        "actors_photos": json.dumps(photos, ensure_ascii=False) if photos else None,
        "cast_json": (
            json.dumps(canonical_cast, ensure_ascii=False, separators=(",", ":"))
            if canonical_cast else None
        ),
        "directors_photos": json.dumps(director_photos, ensure_ascii=False) if director_photos else None,
        # Тип записи сохраняем сразу: без него сериал неотличим от фильма и
        # попадает в подбор ФИЛЬМОВ (см. media_type.py).
        "media_type": media_type.from_kinopoisk(doc),
    }


def _omdb_item(r: dict) -> dict:
    poster = r.get("Poster")
    title = r.get("Title", "")
    return {
        "src": "i",
        "ref": r["imdbID"],
        "imdb_id": r["imdbID"],
        "title": title,
        "title_original": title or None,
        "year": r.get("Year", "?"),
        "poster": poster if poster and poster != "N/A" else None,
        "rating": None,
        "votes": _parse_number(r.get("imdbVotes")),
        "genres": None,
        "type": "series" if r.get("Type") == "series" else "movie",
    }


def _is_sequel(base: str, title: str) -> bool:
    """Нумерованный сиквел (Part II / 2 / 3), без одноимённого мусора."""
    b, t = base.lower().strip(), title.lower().strip()
    if not t.startswith(b):
        return False
    rest = t[len(b):].strip(" :.-")
    if not rest:
        return False
    return bool(re.match(r"^(part\s+)?(\d+|[ivxlc]+)\b", rest))


async def _expand(items: list[dict]) -> list[dict]:
    """Доиск сиквелов по английскому названию топ-результата (OMDb-путь).
    Русские названия частей франшиз разные — иначе трилогии не собираются."""
    top = items[0]
    if top["src"] != "i":
        return items
    try:
        d = await omdb.get_movie(top["ref"])
        eng = (d or {}).get("Title")
        if not eng:
            return items
        more, _, _ = await omdb.search_movies(eng)
    except Exception:
        return items

    seen = {it["ref"] for it in items}
    for r in more:
        imdb = r.get("imdbID")
        if imdb and imdb not in seen and _is_sequel(eng, r.get("Title", "")):
            items.append(_omdb_item(r))
            seen.add(imdb)
    return items


async def _enrich_items(items: list[dict]) -> dict[str, dict]:
    """Дотянуть детали OMDb и вернуть их для постоянного каталога."""
    details: dict[str, dict] = {}

    async def fill(it: dict) -> None:
        if it["src"] != "i":
            return
        try:
            d = await omdb.get_movie(it["ref"])
        except Exception:
            return
        if not d:
            return
        details[it["ref"]] = d
        poster = d.get("Poster")
        if poster and poster != "N/A" and not it.get("poster"):
            it["poster"] = poster
        if not it.get("rating"):
            rt = d.get("imdbRating")
            if rt and rt != "N/A":
                it["rating"] = rt
        if not it.get("votes"):
            it["votes"] = _parse_number(d.get("imdbVotes"))
        g = d.get("Genre")
        if g and g != "N/A":
            it["genres"] = g
        if d.get("Type") == "series":
            it["type"] = "series"

    await asyncio.gather(*[fill(it) for it in items])
    return details


def _omdb_catalog_details(data: dict, title: str, poster_url: str | None) -> dict:
    """Persist a fallback result without spending a second Kinopoisk request."""
    original = data.get("Title", "")
    return {
        "imdb_id": data["imdbID"],
        "title": title,
        "title_original": original if original and original != title else None,
        "year": _clean(data.get("Year")),
        "genres": _clean(data.get("Genre")),
        "directors": _clean(data.get("Director")),
        "actors": _clean(data.get("Actors")),
        "runtime": _clean(data.get("Runtime")),
        "imdb_rating": _clean(data.get("imdbRating")),
        "imdb_votes": _clean(data.get("imdbVotes")),
        "plot": _clean(data.get("Plot")),
        "poster_url": poster_url,
        "media_type": media_type.from_omdb(data),
        # OMDb англоязычен целиком. Его Plot/Genre/Director идут ТОЛЬКО в
        # английские слоты: именно попадание английского текста в русские поля
        # и давало «Crime · Drama · Thriller» в русском интерфейсе.
        "title_en": original or title,
        "plot_en": _clean(data.get("Plot")),
        "genres_en": flang.localized_genres(_clean(data.get("Genre")), "en") or None,
        "genres_ru": flang.localized_genres(_clean(data.get("Genre")), "ru") or None,
        "directors_en": _clean(data.get("Director")),
        # Русское название приходит не от OMDb, а от Wikidata выше по стеку —
        # если оно там нашлось, `title` уже русское.
        "title_ru": title if flang.looks_russian(title) else None,
    }


async def find_movies(query: str) -> list[dict]:
    """Поиск. Возвращает нормализованные item'ы (пустой список = не найдено)."""
    kp_items: list[dict] = []
    kp_docs: list[dict] = []
    if KINOPOISK_TOKEN:
        try:
            kp_docs = [d for d in await kinopoisk.search_movies(query)
                       if d.get("id") and (d.get("name") or d.get("alternativeName"))]
            kp_items = [_kp_item(d) for d in kp_docs]
            kp_credible = _credible_items(kp_items, query)
            kp_meaningful = any(_title_match(item, query)[0] <= 4 for item in kp_items)
            if len(kp_credible) >= 5 and kp_meaningful:
                ranked = _rank_items(kp_credible, query)
                for item in ranked:
                    doc = next((d for d in kp_docs if str(d.get("id")) == item["ref"]), None)
                    if doc:
                        try:
                            await db.get_or_create_film(**_kp_details(doc))
                        except Exception:
                            logger.warning("Could not cache Kinopoisk film %s", doc.get("id"), exc_info=True)
                return ranked
        except Exception as e:
            logger.warning("Kinopoisk search failed, fallback: %s", e)

    results, _translated, _fail = await omdb.search_movies(query)
    omdb_items = [_omdb_item(r) for r in results]
    if omdb_items:
        omdb_items = await _expand(omdb_items)
        omdb_items = omdb_items[:8]

    ru_titles, omdb_details = await asyncio.gather(
        wikidata.get_titles_by_imdb([it["ref"] for it in omdb_items], "ru"),
        _enrich_items(omdb_items),
    )
    for it in omdb_items:
        wt = ru_titles.get(it["ref"])
        if wt and has_cyrillic(wt):
            it["title"] = wt
    merged = _merge_items(kp_items, omdb_items)
    ranked = _rank_items(merged, query)
    for item in ranked:
        if item.get("src") == "k":
            doc = next((d for d in kp_docs if str(d.get("id")) == item["ref"]), None)
            if doc:
                try:
                    await db.get_or_create_film(**_kp_details(doc))
                except Exception:
                    logger.warning("Could not cache Kinopoisk film %s", doc.get("id"), exc_info=True)
        else:
            data = omdb_details.get(item["ref"])
            if data:
                try:
                    await db.get_or_create_film(**_omdb_catalog_details(data, item["title"], item.get("poster")))
                except Exception:
                    logger.warning("Could not cache OMDb film %s", item["ref"], exc_info=True)
    return ranked


async def cached_search(query: str, user_id: int | None = None) -> dict:
    """Cache-first поиск под лимит источника.
    Возвращает {items, cached, limited, throttled}:
      cached    — отдано из кэша (L1/L2), внешний вызов не делался;
      limited   — дневной бюджет исчерпан и свежего кэша нет;
      throttled — пользователь превысил per-user лимит (штрафуем ТОЛЬКО реальные
                  обращения к API — кэш-хиты бесплатны и не throttl-ятся)."""
    normalized = _qnorm(query)
    key = f"{SEARCH_CACHE_VERSION}:{normalized}"
    now = time.time()
    started = time.perf_counter()
    # The permanent catalog is the first layer: once a film entered our database,
    # every future title/actor lookup for it is free forever.
    catalog_candidates = await db.search_catalog(normalized, limit=60)
    catalog_credible = _credible_items(catalog_candidates, normalized)
    catalog_items = _rank_items(catalog_credible, normalized, 8)
    year_query = _query_parts(normalized)[1]
    exact_year_local = any(
        _title_match(item, normalized) == (0, 1)
        for item in catalog_credible
    )
    catalog_complete = len(catalog_credible) >= 5
    if catalog_complete or (year_query and exact_year_local):
        logger.info("search layer=catalog elapsed_ms=%d result_count=%d strong_local=true",
                    int((time.perf_counter() - started) * 1000), len(catalog_items))
        return {"items": catalog_items, "cached": True, "limited": False, "throttled": False}
    # L1: in-memory.
    hit = _QCACHE.get(key)
    if hit and now - hit[0] < _QTTL:
        items = _rank_items(_merge_items(catalog_items, hit[1]), normalized)
        return {"items": items, "cached": True, "limited": False, "throttled": False}
    # L2: постоянный кэш в БД (переживает рестарт/деплой, общий на всех).
    stored = await db.search_cache_get(key, _DB_TTL, _EMPTY_DB_TTL)
    if stored is not None:
        _QCACHE[key] = (now, stored)
        items = _rank_items(_merge_items(catalog_items, stored), normalized)
        return {"items": items, "cached": True, "limited": False, "throttled": False}

    # Дальше — реальный внешний вызов. Per-user throttle считаем только здесь.
    # Catalog reads are complete at this point. Return an idle request DB lease
    # before waiting on providers so their latency cannot occupy a pool slot.
    await db_runtime.release_request_connection_if_idle()
    if user_id is not None and not ratelimit.allow_user(user_id):
        return {"items": [], "cached": False, "limited": False, "throttled": True}

    task = _INFLIGHT.get(key)
    if task is None:
        task = db_runtime.create_background_task(find_movies(query), name=f"search-{normalized}")
        _INFLIGHT[key] = task
    try:
        provider_items = await task
    finally:
        if _INFLIGHT.get(key) is task:
            _INFLIGHT.pop(key, None)
    items = _rank_items(_merge_items(catalog_items, provider_items), normalized)
    _QCACHE[key] = (now, items)
    # Short-lived negative entries are also useful: repeated misspellings or bot
    # probes must not consume a provider call every time.
    await db.search_cache_put(key, items)
    global _puts
    _puts += 1
    if _puts % 100 == 0:  # изредка подчищаем протухший L2
        await purge_expired()
    if len(_QCACHE) > _QMAX:  # простая очистка старейших L1
        for k in sorted(_QCACHE, key=lambda k: _QCACHE[k][0])[:_QMAX // 6]:
            _QCACHE.pop(k, None)
    logger.info("search layer=provider elapsed_ms=%d result_count=%d strong_local=%s",
                int((time.perf_counter() - started) * 1000), len(items), str(catalog_complete).lower())
    return {"items": items, "cached": False, "limited": False, "throttled": False}


async def purge_expired() -> int:
    """Убрать протухшие записи постоянного кэша поиска (на старте и периодически)."""
    removed = await db.purge_search_cache(_DB_TTL)
    if removed:
        logger.info("search_cache: удалено %d протухших записей", removed)
    return removed


def _clean(val):
    return None if val in ("N/A", "", None) else val


async def fetch_details(src: str, ref: str) -> dict | None:
    """Полные данные фильма под database.get_or_create_film."""
    if src == "k":
        doc = await kinopoisk.get_movie(ref)
        if not doc:
            return None
        details = _kp_details(doc)
        imdb_id = details["imdb_id"]
        poster_url = details["poster_url"]
        # У КП постера нет — добираем из OMDb, если есть настоящий imdb.
        if not poster_url and imdb_id.startswith("tt"):
            poster_url = await posters.resolve_omdb(imdb_id)
        details["poster_url"] = poster_url
        return details

    # src == "i": OMDb + официальное русское название из Wikidata.
    data = await omdb.get_movie(ref)
    if not data:
        return None
    original = data.get("Title", "")
    ru_titles = await wikidata.get_titles_by_imdb([data["imdbID"]], "ru")
    title = best_title(ru_titles.get(data["imdbID"]), original)
    # One batched Kinopoisk request is enough for optional visual metadata.
    # If it is unavailable, OMDb remains a complete fallback without a retry loop.
    poster_url = None
    backdrop_url = None
    age_rating = None
    asset = (await kinopoisk.assets_by_imdb([data["imdbID"]])).get(data["imdbID"]) or {}
    poster_url = asset.get("poster_url")
    backdrop_url = asset.get("backdrop_url")
    age_rating = asset.get("age_rating")
    if not poster_url:
        poster_url = omdb.upscale_poster(data.get("Poster"))
    if not age_rating:
        # OMDb Rated — «R»/«PG-13»/«Not Rated»/«N/A»: другая система, но лучше, чем ничего.
        rated = _clean(data.get("Rated"))
        age_rating = rated if rated not in (None, "Not Rated", "Unrated") else None
    return {
        "imdb_id": data["imdbID"],
        # Cross-provider identity is critical: without it the same movie can be
        # stored once as tt... and later again as synthetic kp_....
        "kp_id": asset.get("kp_id"),
        "title": title,
        "title_original": original if original != title else None,
        "year": _clean(data.get("Year")),
        "genres": _clean(data.get("Genre")),
        "directors": _clean(data.get("Director")),
        "actors": _clean(data.get("Actors")),
        "runtime": _clean(data.get("Runtime")),
        "imdb_rating": _clean(data.get("imdbRating")),
        "kp_rating": None,
        "imdb_votes": _clean(data.get("imdbVotes")),
        "plot": _clean(data.get("Plot")),
        "poster_url": poster_url,
        "backdrop_url": backdrop_url,
        "age_rating": age_rating,
        "media_type": media_type.from_omdb(data),
        # См. _omdb_catalog_details: английские данные — только в английские
        # слоты. Русское название здесь берётся из Wikidata (переменная title).
        "title_ru": title if flang.looks_russian(title) else None,
        "title_en": original or None,
        "plot_en": _clean(data.get("Plot")),
        "genres_en": flang.localized_genres(_clean(data.get("Genre")), "en") or None,
        "genres_ru": flang.localized_genres(_clean(data.get("Genre")), "ru") or None,
        "directors_en": _clean(data.get("Director")),
    }
