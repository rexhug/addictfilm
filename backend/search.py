"""Фасад поиска (перенесён из movie_bot, адаптирован под JSON API).

Порядок источников:
  1. kinopoisk.dev — русские названия, постеры, рейтинги КП+IMDb одним запросом.
  2. OMDb + Wikidata — fallback: англ. поиск + официальные рус. названия по SPARQL.

Нормализованный item:
  {src: "k"|"i", ref, title, year, poster, rating, genres, type}
"""
import asyncio
import logging
import re
import time

import kinopoisk
import omdb
import ratelimit
import wikidata
from config import KINOPOISK_TOKEN

logger = logging.getLogger(__name__)

# Кэш результатов поиска по нормализованному запросу — одинаковые/популярные
# запросы (в т.ч. от разных пользователей) не тратят лимит источника.
_QCACHE: dict[str, tuple[float, list]] = {}
_QTTL = 6 * 3600   # свежесть результата, сек
_QMAX = 300        # максимум запросов в кэше


def _qnorm(query: str) -> str:
    return " ".join(query.lower().split())


def extract_imdb_id(text: str) -> str | None:
    m = re.search(r"tt\d{7,8}", text)
    return m.group(0) if m else None


def has_cyrillic(text: str) -> bool:
    return bool(text) and bool(re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", text))


def best_title(wikidata_title: str | None, fallback: str) -> str:
    """Название из Wikidata — только если кириллицей (латиница не понижает хорошее)."""
    return wikidata_title if (wikidata_title and has_cyrillic(wikidata_title)) else fallback


def _kp_item(doc: dict) -> dict:
    poster = (doc.get("poster") or {}).get("url")
    r = doc.get("rating") or {}
    rating = r.get("imdb") or r.get("kp")
    return {
        "src": "k",
        "ref": str(doc["id"]),
        "title": doc.get("name") or doc.get("alternativeName") or "",
        "year": str(doc.get("year") or "?"),
        "poster": poster,
        "rating": f"{rating:.1f}" if rating else None,
        "genres": ", ".join(g["name"] for g in (doc.get("genres") or [])) or None,
        "type": "series" if kinopoisk.is_series(doc) else "movie",
    }


def _omdb_item(r: dict) -> dict:
    poster = r.get("Poster")
    return {
        "src": "i",
        "ref": r["imdbID"],
        "title": r.get("Title", ""),
        "year": r.get("Year", "?"),
        "poster": poster if poster and poster != "N/A" else None,
        "rating": None,
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
    except Exception:  # noqa: BLE001
        return items

    seen = {it["ref"] for it in items}
    for r in more:
        imdb = r.get("imdbID")
        if imdb and imdb not in seen and _is_sequel(eng, r.get("Title", "")):
            items.append(_omdb_item(r))
            seen.add(imdb)
    return items


async def _enrich_items(items: list[dict]) -> None:
    """Дотянуть постер/рейтинг/жанр/тип из OMDb для fallback-результатов (параллельно)."""
    async def fill(it: dict) -> None:
        if it["src"] != "i":
            return
        try:
            d = await omdb.get_movie(it["ref"])
        except Exception:  # noqa: BLE001
            return
        if not d:
            return
        poster = d.get("Poster")
        if poster and poster != "N/A" and not it.get("poster"):
            it["poster"] = poster
        if not it.get("rating"):
            rt = d.get("imdbRating")
            if rt and rt != "N/A":
                it["rating"] = rt
        g = d.get("Genre")
        if g and g != "N/A":
            it["genres"] = g
        if d.get("Type") == "series":
            it["type"] = "series"

    await asyncio.gather(*[fill(it) for it in items])


async def find_movies(query: str) -> list[dict]:
    """Поиск. Возвращает нормализованные item'ы (пустой список = не найдено)."""
    if KINOPOISK_TOKEN:
        try:
            docs = await kinopoisk.search_movies(query)
            items = [_kp_item(d) for d in docs if (d.get("name") or d.get("alternativeName"))]
            if items:
                return items
        except Exception as e:  # noqa: BLE001
            logger.warning("Kinopoisk search failed, fallback: %s", e)

    results, _translated, _fail = await omdb.search_movies(query)
    items = [_omdb_item(r) for r in results]
    if not items:
        return []

    items = await _expand(items)
    items = items[:6]

    ru_titles, _ = await asyncio.gather(
        wikidata.get_titles_by_imdb([it["ref"] for it in items], "ru"),
        _enrich_items(items),
    )
    for it in items:
        wt = ru_titles.get(it["ref"])
        if wt and has_cyrillic(wt):
            it["title"] = wt
    return items


async def cached_search(query: str) -> dict:
    """Cache-first поиск под лимит источника.
    Возвращает {items, cached, limited}:
      cached  — отдано из кэша запросов (внешний вызов не делался);
      limited — дневной бюджет исчерпан и свежего кэша нет (клиенту показать
                «поиск временно ограничен»)."""
    key = _qnorm(query)
    now = time.time()
    hit = _QCACHE.get(key)
    if hit and now - hit[0] < _QTTL:
        return {"items": hit[1], "cached": True, "limited": False}

    if not ratelimit.try_spend_search():
        if hit:  # бюджета нет — отдаём устаревший кэш, лучше чем ничего
            return {"items": hit[1], "cached": True, "limited": False}
        logger.warning("Search budget exhausted, query %r not cached", query)
        return {"items": [], "cached": False, "limited": True}

    items = await find_movies(query)
    _QCACHE[key] = (now, items)
    if len(_QCACHE) > _QMAX:  # простая очистка старейших
        for k in sorted(_QCACHE, key=lambda k: _QCACHE[k][0])[:_QMAX // 6]:
            _QCACHE.pop(k, None)
    return {"items": items, "cached": False, "limited": False}


def _clean(val):
    return None if val in ("N/A", "", None) else val


async def fetch_details(src: str, ref: str) -> dict | None:
    """Полные данные фильма под database.get_or_create_film."""
    if src == "k":
        doc = await kinopoisk.get_movie(ref)
        if not doc:
            return None
        r = doc.get("rating") or {}
        v = doc.get("votes") or {}
        name = doc.get("name") or doc.get("alternativeName") or ""
        original = doc.get("alternativeName")
        length = doc.get("movieLength") or doc.get("seriesLength")
        directors, actors = kinopoisk.extract_credits(doc.get("persons") or [])
        return {
            "imdb_id": kinopoisk.imdb_id_of(doc),
            "title": name,
            "title_original": original if original and original != name else None,
            "year": str(doc["year"]) if doc.get("year") else None,
            "genres": ", ".join(g["name"] for g in (doc.get("genres") or [])) or None,
            "directors": directors,
            "actors": actors,
            "runtime": f"{length} мин" if length else None,
            "imdb_rating": f"{r['imdb']:.1f}" if r.get("imdb") else None,
            "kp_rating": f"{r['kp']:.1f}" if r.get("kp") else None,
            "imdb_votes": str(v["imdb"]) if v.get("imdb") else None,
            "plot": doc.get("description") or doc.get("shortDescription"),
            "poster_url": (doc.get("poster") or {}).get("url"),
        }

    # src == "i": OMDb + официальное русское название из Wikidata.
    data = await omdb.get_movie(ref)
    if not data:
        return None
    original = data.get("Title", "")
    ru_titles = await wikidata.get_titles_by_imdb([data["imdbID"]], "ru")
    title = best_title(ru_titles.get(data["imdbID"]), original)
    return {
        "imdb_id": data["imdbID"],
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
        "poster_url": _clean(data.get("Poster")),
    }
