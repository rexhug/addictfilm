import re
import asyncio
import logging
import aiohttp
from concurrent.futures import ThreadPoolExecutor
from config import OMDB_API_KEY

logger = logging.getLogger(__name__)

OMDB_URL = "https://www.omdbapi.com/"
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)
_executor = ThreadPoolExecutor(max_workers=2)
_session: aiohttp.ClientSession | None = None

try:
    import wikidata as _wikidata
    _HAS_WIKIDATA = True
except Exception:
    _HAS_WIKIDATA = False


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT)
    return _session


async def aclose() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _has_cyrillic(text: str) -> bool:
    return bool(re.search("[а-яА-ЯёЁіІїЇєЄ]", text))


# OMDb отдаёт постеры Amazon в мелком размере (…._V1_SX300.jpg). Amazon-хостинг
# рендерит любой размер по модификатору между «._V1_» и расширением — просим
# ширину пошире, чтобы постер не был мыльным на retina. Не-Amazon URL не трогаем.
_AMZ_POSTER_RE = re.compile(r"(\._V1_).*?(\.(?:jpg|jpeg|png|webp))$", re.IGNORECASE)
_UPSCALE_WIDTH = 600


def upscale_poster(url: str | None, width: int = _UPSCALE_WIDTH) -> str | None:
    """URL постера OMDb/Amazon в бо́льшем разрешении (SX{width}). Прочие URL — как есть."""
    if not url or url == "N/A":
        return None
    return _AMZ_POSTER_RE.sub(rf"\g<1>SX{width}\g<2>", url)


def _translate_sync(text: str) -> str | None:
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target="en").translate(text)
    except Exception:
        return None


async def translate_to_english(text: str) -> str | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _translate_sync, text)


def _split_year(query: str) -> tuple[str, str | None]:
    """Extract trailing 4-digit year from query: 'Obsession 2025' → ('Obsession', '2025')."""
    parts = query.rsplit(None, 1)
    if len(parts) == 2 and re.fullmatch(r"(19|20)\d{2}", parts[1]):
        return parts[0].strip(), parts[1]
    return query, None


async def search_movies(query: str) -> tuple[list[dict], str | None, str | None]:
    """Returns (results, translated_query, fail_reason)."""
    session = await _get_session()
    title, year = _split_year(query)
    try:
        params = {"s": title, "apikey": OMDB_API_KEY}
        if year:
            params["y"] = year
        async with session.get(OMDB_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            if data.get("Response") == "True":
                return data.get("Search", [])[:10], None, None
        # Fallback: retry without year filter if nothing found
        if year:
            params.pop("y")
            async with session.get(OMDB_URL, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                if data.get("Response") == "True":
                    return data.get("Search", [])[:10], None, None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("OMDb search error: %s", e)
        return [], None, "❌ Ошибка соединения с OMDb. Попробуй позже."

    if _has_cyrillic(query):
        if _HAS_WIKIDATA:
            try:
                wd_results = await _wikidata.search_movies(query)
                if wd_results:
                    return wd_results, None, None
            except Exception as e:
                logger.warning("Wikidata search failed: %s", e)

        translated = await translate_to_english(query)
        if translated and translated.lower() != query.lower():
            try:
                async with session.get(OMDB_URL, params={"s": translated, "apikey": OMDB_API_KEY}) as resp2:
                    resp2.raise_for_status()
                    data2 = await resp2.json(content_type=None)
                    if data2.get("Response") == "True":
                        return data2.get("Search", [])[:7], translated, None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("OMDb translated search error: %s", e)

            fail = (
                f"❌ Не найдено.\n"
                f"🔤 Перевёл как <b>{translated}</b> — тоже не найдено.\n\n"
                f"Попробуй написать оригинальное английское название.\n"
                f"Например: <code>The Hangover</code> вместо «Мальчишник в Вегасе»"
            )
            return [], translated, fail

    return [], None, "❌ Не найдено. Проверь название или попробуй по-английски."


# Детали фильма по IMDb ID не меняются — кэшируем в памяти на время жизни бота.
# За один поиск get_movie дёргается несколько раз по одному id (обогащение +
# выбор) — кэш убирает лишние сетевые запросы.
_movie_cache: dict[str, dict] = {}
_CACHE_MAX = 500


async def get_movie(imdb_id: str) -> dict | None:
    cached = _movie_cache.get(imdb_id)
    if cached is not None:
        return cached

    session = await _get_session()
    try:
        params = {"i": imdb_id, "apikey": OMDB_API_KEY, "plot": "short"}
        async with session.get(OMDB_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            if data.get("Response") == "True":
                if len(_movie_cache) >= _CACHE_MAX:
                    _movie_cache.clear()
                _movie_cache[imdb_id] = data
                return data
            return None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("OMDb get_movie error for %s: %s", imdb_id, e)
        return None
