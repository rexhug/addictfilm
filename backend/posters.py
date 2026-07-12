"""Мульти-джерельний резолвер постерів.

Порядок джерел для одного фільму (по imdb_id):
  1. kinopoisk.dev по externalId.imdb — рідні постери КП (часто якісніші й без
     блокувань), один батч-запит на пачку id.
  2. OMDb по imdb_id — fallback; постер Amazon апскейлимо (SX300 → SX600).

Синтетичні id виду `kp_<id>` (фільм без imdb) резолвимо прямим запитом до КП.

Бекфіл проходить каталог `films` без постера: спершу масово добирає постери з
КП одним-двома батчами, лишок домальовує з OMDb поштучно, оновлює БД.
"""
import asyncio
import logging

import database as db
import kinopoisk
import omdb

logger = logging.getLogger(__name__)


async def resolve_omdb(imdb_id: str) -> str | None:
    """Постер OMDb по imdb_id, уже в бо́льшем разрешении. None — если нет/ошибка."""
    data = await omdb.get_movie(imdb_id)
    if not data:
        return None
    return omdb.upscale_poster(data.get("Poster"))


async def resolve(imdb_id: str) -> str | None:
    """Лучший постер для одного фильма. Kinopoisk → OMDb (с апскейлом)."""
    if not imdb_id:
        return None

    # Синтетический id (фильм без imdb) — только прямой запрос к КП по kp-id.
    if imdb_id.startswith("kp_"):
        doc = await kinopoisk.get_movie(imdb_id[3:])
        return (doc.get("poster") or {}).get("url") if doc else None

    kp = await kinopoisk.posters_by_imdb([imdb_id])
    if kp.get(imdb_id):
        return kp[imdb_id]
    return await resolve_omdb(imdb_id)


async def backfill(limit: int = 200, _omdb_cap: int = 60) -> dict:
    """Добрать постеры фильмам каталога без картинки.

    Экономно к лимитам: КП добираем массово (батчи по 40 imdb), остаток тянем
    из OMDb поштучно, но не больше `_omdb_cap` за прогон. Возвращает статистику.
    """
    films = await db.films_missing_poster(limit)
    if not films:
        return {"scanned": 0, "kinopoisk": 0, "omdb": 0, "updated": 0, "remaining": 0}

    real_ids = [f["imdb_id"] for f in films if f["imdb_id"].startswith("tt")]
    synthetic = [f for f in films if not f["imdb_id"].startswith("tt")]

    updated = kp_hits = omdb_hits = 0

    # 1. Массовый добор из Кинопоиска по настоящим imdb id.
    kp_map = await kinopoisk.posters_by_imdb(real_ids)
    for imdb_id, url in kp_map.items():
        if await db.set_film_poster(imdb_id, url):
            updated += 1
            kp_hits += 1

    # 2. Остаток (КП не дал) домалёвываем из OMDb — поштучно и с лимитом.
    still_missing = [i for i in real_ids if i not in kp_map]
    for imdb_id in still_missing[:_omdb_cap]:
        url = await resolve_omdb(imdb_id)
        if url and await db.set_film_poster(imdb_id, url):
            updated += 1
            omdb_hits += 1

    # 3. Синтетические (kp_<id>) — прямым запросом к КП.
    for f in synthetic:
        url = await resolve(f["imdb_id"])
        if url and await db.set_film_poster(f["imdb_id"], url):
            updated += 1
            kp_hits += 1

    remaining = len(films) - updated
    logger.info("Бекфил постеров: просканировано=%d КП=%d OMDb=%d обновлено=%d осталось=%d",
                len(films), kp_hits, omdb_hits, updated, remaining)
    return {"scanned": len(films), "kinopoisk": kp_hits, "omdb": omdb_hits,
            "updated": updated, "remaining": remaining}
