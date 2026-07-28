"""Ручной подбор кадров: ``python -m enrichment.hero_backfill``.

Умолчания консервативные намеренно. Команда ходит во внешний сервис, и её
запускают на проде руками — «по умолчанию весь каталог» здесь было бы ловушкой.

Ключи не печатаются нигде: вывод состоит из названий фильмов, типа изображения,
источника и счёта.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

import database as db
import fanart
from config import FANART_HERO_ENABLED, KINOPOISK_HERO_ENABLED

from . import hero

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Подбор изображений для экрана подбора")
    parser.add_argument("--limit", type=int, default=50, help="сколько фильмов взять всего")
    parser.add_argument("--batch-size", type=int, default=10, help="размер одной пачки")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="одновременных запросов к Fanart (потолок 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать выбор, ничего не записывая")
    parser.add_argument("--film-id", type=int, default=None,
                        help="только этот фильм (для точечной проверки)")
    parser.add_argument("--force", action="store_true",
                        help="не смотреть на срок перепроверки")
    return parser.parse_args(argv)


def _format(outcome: hero.HeroOutcome) -> str:
    score = "—" if outcome.quality_score is None else f"{outcome.quality_score:.3f}"
    size = f"{outcome.width}x{outcome.height}" if outcome.width and outcome.height else "—"
    return (f"[{outcome.action:9}] {outcome.title[:44]:44} "
            f"{outcome.hero_type or '—'!s:12} {outcome.hero_source or '—'!s:10} "
            f"score={score:6} {size:10} кандидатов={outcome.candidates}")


async def _candidates(args: argparse.Namespace) -> list[dict]:
    if args.film_id is not None:
        film = await db.get_film(args.film_id)
        return [film] if film else []
    if args.force:
        # --force означает «не смотреть на срок», а не «взять весь каталог»:
        # limit остаётся жёстким потолком внешних запросов.
        return await db.list_films_for_hero_backfill(limit=max(1, args.limit))
    return await db.list_films_missing_or_stale_hero(limit=max(1, args.limit))


async def run(args: argparse.Namespace) -> hero.HeroReport:
    await db.init_db()
    # Осмотр разрешён и при выключенном флаге: он ничего не пишет, а посмотреть
    # на качество отбора нужно ДО включения. Запись — только с флагом.
    if not (FANART_HERO_ENABLED or KINOPOISK_HERO_ENABLED) and not args.dry_run:
        print("Оба источника выключены (KINOPOISK_HERO_ENABLED / FANART_HERO_ENABLED) — "
              "запись отключена. Посмотреть отбор можно с --dry-run.")
        return hero.HeroReport()
    if FANART_HERO_ENABLED and not fanart.configured():
        print("Ключи Fanart не настроены (FANART_PROJECT_KEY / FANART_CLIENT_KEY).")

    films = await _candidates(args)
    if not films:
        print("Кандидатов нет.")
        return hero.HeroReport()

    total = hero.HeroReport()
    batch_size = max(1, args.batch_size)
    try:
        for start in range(0, len(films), batch_size):
            chunk = films[start:start + batch_size]
            report = await hero.refresh_due_heroes(
                films=chunk, concurrency=args.concurrency, dry_run=args.dry_run)
            for outcome in report.outcomes:
                print(_format(outcome))
                total.add(outcome)
            # Дрожание между пачками: ровный поток запросов к чужому сервису
            # выглядит хуже, чем неровный, и быстрее упирается в лимит.
            if start + batch_size < len(films):
                await asyncio.sleep(1.5)
    finally:
        # Разовая команда обязана закрыть за собой сессию: воркер живёт долго и
        # переиспользует её, а здесь процесс выходит и aiohttp ругается.
        await fanart.close()

    print("\nИтог:", total.as_dict())
    if not args.dry_run:
        print("Каталог:", await db.hero_distribution())
    return total


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                        level=logging.INFO)
    asyncio.run(run(_parse_args(argv)))


if __name__ == "__main__":
    main()
