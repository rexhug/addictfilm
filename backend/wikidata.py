import asyncio
import logging

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
