"""Мульти-джерельний резолвер постерів.

Порядок джерел для одного фільму:
  1. kinopoisk.dev по externalId.imdb — рідні постери КП (часто якісніші й без
     блокувань), один батч-запит на пачку id.
  2. OMDb по imdb_id — fallback; постер Amazon апскейлимо (SX300 → SX600).
  3. kinopoisk-пошук за назвою — остання надія, коли imdb-джерела порожні (OMDb
     часто чіпляє нішевий imdb-запис БЕЗ постера, хоча КП має фільм за назвою).
     Жорсткий захист: беремо постер лише при точному збігу нормалізованої назви
     І року ±1 — щоб не влупити постер однойменного чужого фільму.

Синтетичні id виду `kp_<id>` (фільм без imdb) резолвимо прямим запитом до КП.

Бекфіл проходить каталог `films` без постера: спершу масово добирає постери з
КП одним-двома батчами, лишок домальовує з OMDb / пошуку за назвою поштучно.
"""
import json
import logging
import re

import database as db
import kinopoisk
import omdb
import wikidata

logger = logging.getLogger(__name__)

# Нормализация названий для сравнения: убрать дизамбиг «(фильм, 2025)», пунктуацию,
# дефисы, схлопнуть пробелы, привести к нижнему регистру.
_DISAMBIG_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _norm_title(t: str | None) -> str:
    t = _DISAMBIG_RE.sub(" ", (t or "").lower())
    t = _PUNCT_RE.sub(" ", t)
    return " ".join(t.split())


def _year_int(y) -> int | None:
    m = _YEAR_RE.search(str(y or ""))
    return int(m.group(0)) if m else None


async def resolve_omdb(imdb_id: str) -> str | None:
    """Постер OMDb по imdb_id, уже в бо́льшем разрешении. None — если нет/ошибка."""
    data = await omdb.get_movie(imdb_id)
    if not data:
        return None
    return omdb.upscale_poster(data.get("Poster"))


async def resolve_by_name(title: str | None, year=None, original: str | None = None) -> str | None:
    """Последняя надежда: постер по kinopoisk-поиску названия. Возвращает постер
    только при ТОЧНОМ совпадении нормализованного названия и года ±1 — иначе
    рискуем взять картинку однофамильца. None — если уверенного совпадения нет."""
    qn = _norm_title(title)
    qy = _year_int(year)
    if not qn:
        return None
    # Запросы к КП строим из «чистых» названий (без дизамбига «(фильм, 2025)»).
    # Год добавляем в запрос — иначе нишевый фильм тонет под популярными
    # одноимёнными и не попадает в топ выдачи.
    tried: set[str] = set()
    queries = []
    for raw in (title, original):
        base = " ".join(_DISAMBIG_RE.sub(" ", raw or "").split()).strip()
        if not base:
            continue
        for q in ((f"{base} {qy}" if qy else None), base):
            if q and q not in tried:
                tried.add(q)
                queries.append(q)
    for q in queries:
        for d in await kinopoisk.search_movies(q, limit=6):
            url = (d.get("poster") or {}).get("url")
            if not url:
                continue
            cand = _norm_title(d.get("name") or d.get("alternativeName"))
            cy = _year_int(d.get("year"))
            if cand == qn and qy and cy and abs(cy - qy) <= 1:
                logger.info("Постер по названию: %r (%s) → kp id=%s", title, year, d.get("id"))
                return url
    return None


async def resolve(imdb_id: str, title: str | None = None,
                  year=None, original: str | None = None) -> str | None:
    """Лучший постер для одного фильма. Kinopoisk — приоритет (заметно лучше
    качеством): по imdb, затем поиском по названию. OMDb — последний резерв,
    только если кинопоиск фильм фактически не знает ни по imdb, ни по названию."""
    # Синтетический id (фильм без imdb) — прямой запрос к КП по kp-id.
    if imdb_id and imdb_id.startswith("kp_"):
        doc = await kinopoisk.get_movie(imdb_id[3:])
        url = (doc.get("poster") or {}).get("url") if doc else None
        if url:
            return url
    elif imdb_id:
        kp = await kinopoisk.posters_by_imdb([imdb_id])
        if kp.get(imdb_id):
            return kp[imdb_id]

    if title:
        url = await resolve_by_name(title, year, original)
        if url:
            return url

    if imdb_id and imdb_id.startswith("tt"):
        return await resolve_omdb(imdb_id)
    return None


async def backfill(limit: int = 200, _omdb_cap: int = 60) -> dict:
    """Добрать постеры фильмам каталога без картинки.

    Экономно к лимитам: КП добираем массово (батчи по 40 imdb), остаток тянем
    поштучно (OMDb + поиск по названию), но не больше `_omdb_cap` фильмов за
    прогон. Возвращает статистику по источникам.
    """
    films = await db.films_missing_poster(limit)
    if not films:
        return {"scanned": 0, "kinopoisk": 0, "omdb": 0, "by_name": 0, "updated": 0, "remaining": 0}

    real = [f for f in films if f["imdb_id"].startswith("tt")]
    synthetic = [f for f in films if not f["imdb_id"].startswith("tt")]

    updated = kp_hits = omdb_hits = name_hits = 0

    # 1. Массовый добор из Кинопоиска по настоящим imdb id.
    kp_map = await kinopoisk.posters_by_imdb([f["imdb_id"] for f in real])
    for imdb_id, url in kp_map.items():
        if await db.set_film_poster(imdb_id, url):
            updated += 1
            kp_hits += 1

    # 2. Остаток (КП по imdb не дал) — поштучно: поиск в кинопоиске по названию
    # (тоже кинопоисковское качество), OMDb — только если и это не помогло.
    still = [f for f in real if f["imdb_id"] not in kp_map]
    for f in still[:_omdb_cap]:
        url = await resolve_by_name(f["title"], f["year"], f["title_original"])
        src = "name"
        if not url:
            url = await resolve_omdb(f["imdb_id"])
            src = "omdb"
        if url and await db.set_film_poster(f["imdb_id"], url):
            updated += 1
            omdb_hits += src == "omdb"
            name_hits += src == "name"

    # 3. Синтетические (kp_<id>) — прямым запросом к КП (+ добор по названию).
    for f in synthetic:
        url = await resolve(f["imdb_id"], f["title"], f["year"], f["title_original"])
        if url and await db.set_film_poster(f["imdb_id"], url):
            updated += 1
            kp_hits += 1

    remaining = len(films) - updated
    logger.info("Бекфил постеров: скан=%d КП=%d OMDb=%d поназв=%d обновлено=%d осталось=%d",
                len(films), kp_hits, omdb_hits, name_hits, updated, remaining)
    return {"scanned": len(films), "kinopoisk": kp_hits, "omdb": omdb_hits,
            "by_name": name_hits, "updated": updated, "remaining": remaining}


async def upgrade_omdb_posters(limit: int = 200, _name_cap: int = 60) -> dict:
    """Заменить постеры Amazon/OMDb на kinopoisk-версии (заметно лучше качеством)
    у уже добавленных фильмов — догоняет фильмы, попавшие в каталог до фикса
    приоритета источников. Затирает только когда находит kinopoisk-замену;
    если кинопоиск фильм не знает — OMDb-постер остаётся (лучше, чем ничего).
    """
    films = await db.films_with_omdb_poster(limit)
    if not films:
        return {"scanned": 0, "upgraded": 0, "kept_omdb": 0}

    upgraded = 0
    real_ids = [f["imdb_id"] for f in films if f["imdb_id"].startswith("tt")]

    # 1. Массовый добор из кинопоиска по imdb.
    kp_map = await kinopoisk.posters_by_imdb(real_ids)
    for imdb_id, url in kp_map.items():
        if await db.upgrade_film_poster(imdb_id, url):
            upgraded += 1

    # 2. Остаток — поштучно поиском по названию (с лимитом за прогон).
    still = [f for f in films if f["imdb_id"] not in kp_map][:_name_cap]
    for f in still:
        url = await resolve_by_name(f["title"], f["year"], f["title_original"])
        if url and await db.upgrade_film_poster(f["imdb_id"], url):
            upgraded += 1

    kept = len(films) - upgraded
    logger.info("Апгрейд OMDb→kinopoisk: скан=%d апгрейжено=%d осталось на OMDb=%d",
                len(films), upgraded, kept)
    return {"scanned": len(films), "upgraded": upgraded, "kept_omdb": kept}


async def backfill_actor_photos(limit: int = 200) -> dict:
    """Research cast portraits from Wikidata/Commons for older catalogue films.

    The former backfill only filled empty ``actors_photos`` with the Kinopoisk
    ``persons.photo`` value. That misses records where Kinopoisk supplied its
    gray K-card as a nominally valid image. The new pass is idempotent and uses
    no Kinopoisk budget; it also refreshes an already-present but weak cast.
    """
    films = await db.films_needing_actor_photo_enrichment(limit)
    if not films:
        return {"scanned": 0, "wikidata": 0, "checked_empty": 0, "remaining": 0}

    enriched = checked_empty = 0
    for start in range(0, len(films), 20):
        chunk = films[start:start + 20]
        cast_map = await wikidata.get_cast_by_imdb([film["imdb_id"] for film in chunk])
        for film in chunk:
            cast = cast_map.get(film["imdb_id"], [])
            if cast:
                actors = ", ".join(person["name"] for person in cast)
                if await db.set_film_cast_from_wikidata(
                    film["id"], actors, json.dumps(cast, ensure_ascii=False),
                ):
                    enriched += 1
            elif await db.mark_film_actor_photos_checked(film["id"]):
                checked_empty += 1

    remaining = len(films) - enriched - checked_empty
    logger.info("Бекфіл акторів Wiki/Commons: скан=%d обогащено=%d пусто=%d осталось=%d",
                len(films), enriched, checked_empty, remaining)
    return {"scanned": len(films), "wikidata": enriched,
            "checked_empty": checked_empty, "remaining": remaining}
