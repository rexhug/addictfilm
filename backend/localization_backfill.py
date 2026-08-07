"""Разложить накопленный каталог по языковым колонкам.

    python -m localization_backfill --all
    python -m localization_backfill --film-id 42
    python -m localization_backfill --limit 50

Порядок принципиален и экономит бюджет провайдера:

1. Сначала СЕМЕНА из того, что уже лежит в базе. Ни одного внешнего запроса:
   русский текст из общих колонок уходит в русские слоты, латиница — в
   английские, жанры собираются из канона сразу на обоих языках. На каталоге,
   собранном из kinopoisk, это закрывает почти всё.
2. Только потом провайдеры и только за тем, чего не хватило.

Украинский текст не принимается в русский слот ни на каком шаге: именно он и
выглядел в русском интерфейсе как поломка перевода.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

import database as db
import film_localization as flang
import kinopoisk
import omdb
import search
import wikidata

logger = logging.getLogger(__name__)

# Провайдеры отвечают медленно и по чужим лимитам; две параллельные задачи —
# компромисс между временем прогона и вежливостью к чужому сервису.
_PROVIDER_CONCURRENCY = 2


def seed_from_catalog(film: dict) -> dict:
    """Что можно разложить БЕЗ единого внешнего запроса."""
    title = film.get("title")
    original = film.get("title_original")
    plot = film.get("plot")
    directors = film.get("directors")
    genres = film.get("genres")
    seeded: dict[str, str] = {}

    def offer(column: str, value: object) -> None:
        text = str(value or "").strip()
        language = column.rsplit("_", 1)[1]
        if text and flang.accepts(text, language):
            seeded.setdefault(column, text)

    offer("title_ru", title)
    offer("title_en", original)
    # Оригинальное название часто английское и лежит в title, если фильм пришёл
    # из OMDb, — тогда русского названия у нас просто нет, и выдумывать нельзя.
    offer("title_en", title)
    offer("title_ru", original)
    offer("plot_ru", plot)
    offer("plot_en", plot)
    offer("directors_ru", directors)
    offer("directors_en", directors)
    # Жанры детерминированы: канон уже русский, английская витрина — таблица.
    if genres:
        for language in ("ru", "en"):
            display = flang.localized_genres(genres, language)
            if display:
                seeded[f"genres_{language}"] = display
    return seeded


def _needs(film: dict, column: str) -> bool:
    """Слот пуст или заполнен не тем языком."""
    value = str(film.get(column) or "").strip()
    if not value:
        return True
    if column.startswith("genres_"):
        return False
    return not flang.accepts(value, column.rsplit("_", 1)[1])


async def fetch_missing(film: dict) -> dict:
    """Провайдеры — только за недостающим. Пусто = ничего не потребовалось."""
    found: dict[str, str] = {}
    kp_id = str(film.get("kp_id") or "").strip()
    imdb_id = str(film.get("imdb_id") or "").strip()

    ru_missing = [c for c in ("title_ru", "plot_ru", "directors_ru") if _needs(film, c)]
    if ru_missing and kp_id:
        doc = await kinopoisk.get_movie(kp_id)
        if doc:
            details = search._kp_details(doc)
            for column in ("title_ru", "plot_ru", "directors_ru",
                           "title_en", "directors_en", "genres_ru", "genres_en"):
                if details.get(column):
                    found.setdefault(column, details[column])
    elif ru_missing and imdb_id.startswith("tt") and _needs(film, "title_ru"):
        # Русского названия иногда хватает и без kp_id — Wikidata его знает и
        # не тратит бюджет kinopoisk.
        titles = await wikidata.get_titles_by_imdb([imdb_id], "ru")
        candidate = titles.get(imdb_id)
        if flang.looks_russian(candidate):
            found.setdefault("title_ru", candidate)

    en_missing = [c for c in ("title_en", "plot_en", "directors_en") if _needs(film, c)]
    if en_missing and imdb_id.startswith("tt"):
        data = await omdb.get_movie(imdb_id)
        if data:
            for column, key in (("title_en", "Title"), ("plot_en", "Plot"),
                                ("directors_en", "Director")):
                value = search._clean(data.get(key))
                if value:
                    found.setdefault(column, value)
            genre = search._clean(data.get("Genre"))
            if genre:
                found.setdefault("genres_en", flang.localized_genres(genre, "en"))
                found.setdefault("genres_ru", flang.localized_genres(genre, "ru"))
    return found


async def localize_film(film: dict, *, use_providers: bool = True) -> dict:
    """Починить один фильм. Возвращает отчёт по этапам."""
    film_id = int(film["id"])
    seeded = seed_from_catalog(film)
    changed = await db.repair_film_localization(film_id, seeded)
    merged = {**film, **{k: v for k, v in seeded.items() if v}}

    fetched: dict[str, str] = {}
    if use_providers and any(_needs(merged, c) for c in
                             ("title_ru", "plot_ru", "directors_ru",
                              "title_en", "plot_en", "directors_en")):
        fetched = await fetch_missing(merged)
        if fetched and await db.repair_film_localization(film_id, fetched):
            changed = True
    if changed:
        await db.rebuild_film_search_text(film_id)
    return {"film_id": film_id, "title": film.get("title"),
            "seeded": sorted(seeded), "fetched": sorted(fetched), "changed": changed}


async def run(args: argparse.Namespace) -> dict:
    await db.init_db()
    if args.film_id is not None:
        films = await db.films_missing_localization(film_id=args.film_id)
    else:
        films = await db.films_missing_localization(
            limit=10_000 if args.all else max(1, args.limit))
    if not films:
        print("Кандидатов нет.")
        return {"scanned": 0, "changed": 0}

    changed = 0
    semaphore = asyncio.Semaphore(_PROVIDER_CONCURRENCY)

    async def one(film: dict) -> dict:
        async with semaphore:
            try:
                return await localize_film(film, use_providers=not args.seed_only)
            except Exception:
                logger.warning("localization: фильм %s не обработан", film.get("id"), exc_info=True)
                return {"film_id": film.get("id"), "changed": False,
                        "seeded": [], "fetched": [], "title": film.get("title")}

    for start in range(0, len(films), 20):
        chunk = films[start:start + 20]
        for outcome in await asyncio.gather(*(one(f) for f in chunk)):
            if outcome["changed"]:
                changed += 1
                if args.verbose:
                    print(f"[{outcome['film_id']:5}] {str(outcome['title'])[:40]:40} "
                          f"seed={','.join(outcome['seeded']) or '—'} "
                          f"prov={','.join(outcome['fetched']) or '—'}")

    report = {"scanned": len(films), "changed": changed}
    print("\nИтог:", report)
    print("Качество:", await db.localization_quality_report())
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Локализация метаданных каталога")
    parser.add_argument("--film-id", type=int, default=None, help="только этот фильм")
    parser.add_argument("--limit", type=int, default=50, help="сколько фильмов взять")
    parser.add_argument("--all", action="store_true", help="весь каталог")
    parser.add_argument("--seed-only", action="store_true",
                        help="без внешних запросов: только то, что уже есть в базе")
    parser.add_argument("--verbose", action="store_true", help="печатать каждый фильм")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                        level=logging.INFO)
    asyncio.run(run(_parse_args(argv)))


if __name__ == "__main__":
    main()
