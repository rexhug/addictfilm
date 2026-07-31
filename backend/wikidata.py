import logging
import re
from urllib.parse import quote, unquote, urlparse

import aiohttp

logger = logging.getLogger(__name__)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
FILM_TYPES = {"Q11424", "Q24862", "Q506240", "Q1366112", "Q2431196"}
TIMEOUT = aiohttp.ClientTimeout(total=6)
# Wikimedia требует User-Agent с контактом (URL/email), иначе отдаёт 403.
_HEADERS = {
    "User-Agent": "MovieBot/1.0 (https://github.com/rexhug/movie_bot; personal Telegram bot) python-aiohttp"
}
_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers=_HEADERS, timeout=TIMEOUT)
    return _session


async def aclose() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _detect_lang(text: str) -> str:
    return "uk" if any(c in text for c in "іІїЇєЄ") else "ru"


async def get_titles_by_imdb(imdb_ids: list[str], lang: str = "ru") -> dict[str, str]:
    """Официальные названия фильмов на нужном языке по их IMDb ID.

    Один SPARQL-запрос на весь список. Возвращает {imdb_id: title}.
    При любой ошибке/таймауте — пустой словарь (мягкий откат на перевод).
    """
    ids = [i for i in imdb_ids if i and i.startswith("tt")]
    if not ids:
        return {}

    values = " ".join(f'"{i}"' for i in ids)
    query = (
        "SELECT ?id ?label WHERE {"
        f"  VALUES ?id {{ {values} }}"
        "  ?item wdt:P345 ?id ."
        f'  ?item rdfs:label ?label . FILTER(LANG(?label) = "{lang}")'
        "}"
    )
    try:
        session = await _get_session()
        async with session.get(
            WIKIDATA_SPARQL,
            params={"query": query, "format": "json"},
        ) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json(content_type=None)
    except Exception:
        logger.debug("Wikidata get_titles_by_imdb failed", exc_info=True)
        return {}

    out: dict[str, str] = {}
    for row in data.get("results", {}).get("bindings", []):
        imdb_id = row.get("id", {}).get("value")
        label = row.get("label", {}).get("value")
        if imdb_id and label and imdb_id not in out:  # первое совпадение
            out[imdb_id] = label
    return out


def commons_thumbnail_url(raw_url: str | None, width: int = 330) -> str | None:
    """Turn a Wikidata P18 value into a stable, modest-size Commons image URL.

    Wikidata normally returns ``Special:FilePath`` URLs. Keeping that endpoint
    rather than constructing the upload-path hash ourselves lets Wikimedia own
    redirects and filename edge cases; the image proxy validates every redirect.
    """
    if not raw_url:
        return None
    parsed = urlparse(raw_url)
    marker = "/wiki/Special:FilePath/"
    if parsed.hostname not in {"commons.wikimedia.org", "www.commons.wikimedia.org"} or marker not in parsed.path:
        return None
    filename = unquote(parsed.path.split(marker, 1)[1]).strip()
    if not filename or "/" in filename or "\\" in filename:
        return None
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(filename, safe='')}?width={width}"


def _cast_ordinal(value: str | None) -> tuple[int, str]:
    """Wikidata casts use strings such as 1, 2a or 10; keep a natural order."""
    text = str(value or "")
    match = re.search(r"\d+", text)
    return (int(match.group(0)) if match else 10_000, text)


def _cast_from_bindings(rows: list[dict], max_actors: int) -> dict[str, list[dict]]:
    grouped: dict[str, list[tuple[tuple[int, str], str, dict]]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        imdb_id = row.get("imdb", {}).get("value")
        actor_id = row.get("actor", {}).get("value")
        name = row.get("actorLabel", {}).get("value")
        if not imdb_id or not actor_id or not name or (imdb_id, actor_id) in seen:
            continue
        seen.add((imdb_id, actor_id))
        photo_url = commons_thumbnail_url(row.get("image", {}).get("value"))
        grouped.setdefault(imdb_id, []).append((
            _cast_ordinal(row.get("ordinal", {}).get("value")), actor_id,
            # wikidata_id — устойчивая личность. Сопоставление по имени ловит
            # тёзок, а их в кино много; идентификатор не ловит никогда.
            {"name": name, "photo_url": photo_url, "source": "wikidata",
             "wikidata_id": actor_id.rsplit("/", 1)[-1] if actor_id else None},
        ))
    return {
        imdb_id: [entry for _, _, entry in sorted(cast, key=lambda item: (item[0], item[1]))[:max_actors]]
        for imdb_id, cast in grouped.items()
    }


def portrait_candidates_for_cast(cast: list[dict], researched: list[dict]) -> list[dict]:
    """Обогатить ПОРТРЕТЫ канонического состава, не трогая всё остальное.

    Wikidata здесь не имеет права ни переставить актёров, ни переименовать их,
    ни изменить роль, ни добавить человека в состав: она знает свой порядок
    съёмок, а не порядок титров этого фильма. Единственное, что она приносит, —
    ещё одна ссылка на портрет.

    Сопоставляем по устойчивому идентификатору, а если его нет — по ТОЧНОМУ
    нормализованному имени, и только когда оно однозначно с обеих сторон.
    Похожие имена не сближаем: «почти тот же человек» в титрах не бывает.
    """
    def key(value: str | None) -> str:
        return " ".join(str(value or "").split()).casefold()

    by_id = {str(item.get("wikidata_id")): item for item in researched if item.get("wikidata_id")}
    name_counts: dict[str, int] = {}
    for item in researched:
        for value in (item.get("name"), item.get("original_name")):
            if key(value):
                name_counts[key(value)] = name_counts.get(key(value), 0) + 1
    by_unique_name = {}
    for item in researched:
        for value in (item.get("name"), item.get("original_name")):
            if key(value) and name_counts.get(key(value)) == 1:
                by_unique_name.setdefault(key(value), item)

    enriched: list[dict] = []
    for person in cast:
        match = by_id.get(str(person.get("wikidata_id"))) if person.get("wikidata_id") else None
        if match is None:
            own = [key(person.get("name")), key(person.get("original_name"))]
            unique_own = [value for value in own if value and own.count(value) == 1]
            for value in unique_own:
                match = by_unique_name.get(value)
                if match is not None:
                    break
        updated = dict(person)
        if match is not None:
            # Порядок кандидатов — это лицензия, а не приоритет источника в
            # составе: свободный Commons идёт первым, каталожный остаётся
            # запасным. На ПОРЯДОК АКТЁРОВ это не влияет никак.
            urls = [match.get("photo_url"), updated.get("photo_url"),
                    *(updated.get("fallback_photo_urls") or [])]
            ordered: list[str] = []
            for url in urls:
                text = str(url or "").strip()
                if text.startswith("https://") and text not in ordered:
                    ordered.append(text)
            updated["photo_url"] = ordered[0] if ordered else None
            updated["fallback_photo_urls"] = ordered[1:]
            if match.get("wikidata_id") and not updated.get("wikidata_id"):
                updated["wikidata_id"] = match["wikidata_id"]
        enriched.append(updated)
    return enriched


async def get_cast_by_imdb(imdb_ids: list[str], max_actors: int = 10) -> dict[str, list[dict]]:
    """Get top-billed cast and freely licensed portraits from Wikidata/Commons.

    This is deliberately batched and cached by the caller: one SPARQL request
    can enrich several catalogue films without touching the Kinopoisk quota.
    Records without a Commons portrait are still returned, so the UI has the
    right names and falls back to initials only when no real image exists.
    """
    ids = list(dict.fromkeys(item for item in imdb_ids if re.fullmatch(r"tt\d{5,12}", item or "")))
    if not ids:
        return {}

    values = " ".join(f'"{item}"' for item in ids)
    query = (
        "SELECT ?imdb ?actor ?actorLabel ?image ?ordinal WHERE {"
        f"  VALUES ?imdb {{ {values} }}"
        "  ?film wdt:P345 ?imdb ."
        "  ?film p:P161 ?castStatement ."
        "  ?castStatement ps:P161 ?actor ."
        "  OPTIONAL { ?castStatement pq:P1545 ?ordinal . }"
        "  OPTIONAL { ?actor wdt:P18 ?image . }"
        "  SERVICE wikibase:label { bd:serviceParam wikibase:language \"ru,en\". }"
        "} ORDER BY ?imdb ?ordinal"
    )
    try:
        session = await _get_session()
        async with session.get(
            WIKIDATA_SPARQL,
            params={"query": query, "format": "json"},
        ) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json(content_type=None)
    except Exception:
        logger.debug("Wikidata get_cast_by_imdb failed", exc_info=True)
        return {}

    return _cast_from_bindings(data.get("results", {}).get("bindings", []), max_actors)


def _directors_from_bindings(rows: list[dict]) -> dict[str, list[dict]]:
    """Normalize director query rows into the same name/photo contract as cast."""
    grouped: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        imdb_id = row.get("imdb", {}).get("value")
        director_id = row.get("director", {}).get("value")
        name = row.get("directorLabel", {}).get("value")
        if not imdb_id or not director_id or not name or (imdb_id, director_id) in seen:
            continue
        seen.add((imdb_id, director_id))
        grouped.setdefault(imdb_id, []).append({
            "name": name,
            "photo_url": commons_thumbnail_url(row.get("image", {}).get("value")),
            "source": "wikidata",
        })
    return grouped


async def get_directors_by_imdb(imdb_ids: list[str]) -> dict[str, list[dict]]:
    """Get film directors and freely licensed Commons portraits in one batch."""
    ids = list(dict.fromkeys(item for item in imdb_ids if re.fullmatch(r"tt\d{5,12}", item or "")))
    if not ids:
        return {}

    values = " ".join(f'"{item}"' for item in ids)
    query = (
        "SELECT ?imdb ?director ?directorLabel ?image WHERE {"
        f"  VALUES ?imdb {{ {values} }}"
        "  ?film wdt:P345 ?imdb ."
        "  ?film wdt:P57 ?director ."
        "  OPTIONAL { ?director wdt:P18 ?image . }"
        "  SERVICE wikibase:label { bd:serviceParam wikibase:language \"ru,en\". }"
        "} ORDER BY ?imdb ?directorLabel"
    )
    try:
        session = await _get_session()
        async with session.get(
            WIKIDATA_SPARQL,
            params={"query": query, "format": "json"},
        ) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json(content_type=None)
    except Exception:
        logger.debug("Wikidata get_directors_by_imdb failed", exc_info=True)
        return {}

    return _directors_from_bindings(data.get("results", {}).get("bindings", []))


async def search_movies(query: str) -> list[dict]:
    """Шукає фільми на Wikidata за назвою в укр/рос.
    Повертає list[{"Title", "Year", "imdbID"}] — сумісно з форматом OMDb.
    """
    lang = _detect_lang(query)
    try:
        session = await _get_session()

        async with session.get(
            WIKIDATA_API,
            params={"action": "wbsearchentities", "search": query,
                    "language": lang, "type": "item", "limit": 10, "format": "json"},
        ) as resp:
            data = await resp.json(content_type=None)

        search_hits = data.get("search", [])
        if not search_hits:
            return []

        entity_ids = [r["id"] for r in search_hits[:10]]
        labels = {r["id"]: r.get("label", "") for r in search_hits[:10]}

        async with session.get(
            WIKIDATA_API,
            params={"action": "wbgetentities", "ids": "|".join(entity_ids),
                    "props": "claims", "format": "json"},
        ) as resp2:
            data2 = await resp2.json(content_type=None)

    except Exception:
        logger.debug("Wikidata search_movies failed", exc_info=True)
        return []

    results = []
    for eid, entity in data2.get("entities", {}).items():
        if entity.get("missing"):
            continue

        claims = entity.get("claims", {})

        # Перевіряємо що це фільм
        p31_ids = set()
        for c in claims.get("P31", []):
            try:
                p31_ids.add(c["mainsnak"]["datavalue"]["value"]["id"])
            except (KeyError, TypeError):
                pass
        if not p31_ids & FILM_TYPES:
            continue

        # IMDb ID (P345)
        imdb_id = None
        for c in claims.get("P345", []):
            try:
                imdb_id = c["mainsnak"]["datavalue"]["value"]
                break
            except (KeyError, TypeError):
                pass
        if not imdb_id:
            continue

        # Рік виходу (P577)
        year = "?"
        for c in claims.get("P577", []):
            try:
                year = c["mainsnak"]["datavalue"]["value"]["time"][1:5]
                break
            except (KeyError, TypeError):
                pass

        results.append({"Title": labels.get(eid, ""), "Year": year, "imdbID": imdb_id})

    return results[:7]
