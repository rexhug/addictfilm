#!/usr/bin/env python3
"""Ідемпотентний імпорт старого ``movie_bot`` у схему Mini App.

Джерело відкривається SQLite у режимі ``read-only``. Приймачем може бути
локальна SQLite або production PostgreSQL/Neon через ``DATABASE_URL``.
Наявні дані Mini App ніколи не перезаписуються: кожен INSERT має
``ON CONFLICT DO NOTHING``. Тому безпечно спочатку запустити ``--dry-run``, а
потім повторювати імпорт — повторний запуск додасть нуль рядків.

Приклад:
  python scripts/import_movie_bot.py --source /path/to/movies.db --dry-run
  DATABASE_URL=... python scripts/import_movie_bot.py --source /path/to/movies.db --allow-postgres

Для PostgreSQL потрібен явний ``--allow-postgres``. Це захист від випадкового
запуску мігратора проти живої бази. Резервні копії Neon виконує провайдер;
SQLite перед записом копіюється засобами застосунку.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "backend"))

import database as db  # noqa: E402
import db_runtime  # noqa: E402


SRC_DEFAULT = "/Users/denyszapriahailo/movie_bot/movies.db"
# Два користувачі старого спільного бота. За потреби мапінг можна змінити тут,
# не торкаючись вихідної бази.
USERS = {1001453723: "Денис", 5310882391: "Котятко"}
REQUIRED_TABLES = {"movies", "ratings", "comments"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() else default


def read_source(path: str) -> tuple[list[sqlite3.Row], dict[tuple[int, int], int], dict[tuple[int, int], str]]:
    """Зчитати стару базу без жодних write-lock або змін."""
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    with sqlite3.connect(uri, uri=True) as source:
        source.row_factory = sqlite3.Row
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError(f"У джерелі немає таблиць: {', '.join(sorted(missing))}")
        movies = source.execute("SELECT * FROM movies").fetchall()
        ratings = {
            (int(row["user_id"]), int(row["movie_id"])): int(row["rating"])
            for row in source.execute("SELECT movie_id, user_id, rating FROM ratings")
            if row["rating"] is not None and 1 <= int(row["rating"]) <= 10
        }
        comments = {
            (int(row["user_id"]), int(row["movie_id"])): str(row["text"])
            for row in source.execute("SELECT movie_id, user_id, text FROM comments")
            if row["text"] is not None
        }
    return movies, ratings, comments


async def import_records(
    movies: list[sqlite3.Row], ratings: dict[tuple[int, int], int], comments: dict[tuple[int, int], str]
) -> tuple[dict[str, int], dict[str, int]]:
    """Імпортувати записи, зберігаючи наявні дані приймача незмінними."""
    await db_runtime.start(db.DATABASE_URL)
    await db.init_db()
    now = _now()
    new = {"users": 0, "films": 0, "user_films": 0}

    # get_or_create_film вже має portable dedup по imdb_id і коректно обробляє
    # гонку між інстансами PostgreSQL.
    films: list[tuple[int, sqlite3.Row]] = []
    for movie in movies:
        old_id = int(_source_value(movie, "id"))
        imdb_id = _source_value(movie, "imdb_id") or f"legacy_movie_bot_{old_id}"
        existed = await db.get_film_id_by_imdb(str(imdb_id))
        film_id = await db.get_or_create_film(
            str(imdb_id),
            str(_source_value(movie, "title") or "Без назви"),
            year=_source_value(movie, "year"),
            genres=_source_value(movie, "genres"),
            runtime=_source_value(movie, "runtime"),
            imdb_rating=_source_value(movie, "imdb_rating"),
            imdb_votes=_source_value(movie, "imdb_votes"),
            plot=_source_value(movie, "plot"),
            poster_url=_source_value(movie, "poster_url"),
            title_original=_source_value(movie, "title_original"),
            kp_rating=_source_value(movie, "kp_rating"),
            directors=_source_value(movie, "directors"),
            actors=_source_value(movie, "actors"),
        )
        new["films"] += int(existed is None)
        films.append((film_id, movie))

    # Одна транзакція для користувачів і їхніх списків. ON CONFLICT гарантує,
    # що імпорт не зітре новішу оцінку, коментар або статус у Mini App.
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as target:
        for user_id, name in USERS.items():
            cur = await target.execute(
                "INSERT INTO users (id, first_name, username, created_at, last_seen) "
                "VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (user_id, name, None, now, now),
            )
            new["users"] += cur.rowcount

        for film_id, movie in films:
            old_id = int(_source_value(movie, "id"))
            status = _source_value(movie, "status")
            status = status if status in {"watched", "want_to_watch"} else "want_to_watch"
            added_at = _source_value(movie, "added_at") or now
            watched_at = _source_value(movie, "watched_at") if status == "watched" else None
            for user_id in USERS:
                rating = ratings.get((user_id, old_id))
                comment = comments.get((user_id, old_id))
                cur = await target.execute(
                    "INSERT INTO user_films "
                    "(user_id, film_id, status, rating, comment, added_at, watched_at, rated_at) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id, film_id) DO NOTHING",
                    (user_id, film_id, status, rating, comment, added_at, watched_at,
                     now if rating is not None else None),
                )
                new["user_films"] += cur.rowcount
        await target.commit()

    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as target:
        totals = {}
        for table in ("users", "films", "user_films"):
            cur = await target.execute(f"SELECT COUNT(*) FROM {table}")
            totals[table] = int((await cur.fetchone())[0])
    return new, totals


async def prepare_sqlite_backup() -> str | None:
    """Створити схему, якщо це перший запуск, і лише тоді зняти бекап."""
    await db.init_db()
    return await db.backup_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="Імпорт movie_bot → Mini App")
    parser.add_argument("--dry-run", action="store_true", help="лише показати план, нічого не писати")
    parser.add_argument("--source", default=SRC_DEFAULT, help="шлях до movies.db старого бота")
    parser.add_argument("--allow-postgres", action="store_true", help="явно дозволити запис у PostgreSQL/Neon")
    args = parser.parse_args()

    if not os.path.isfile(args.source):
        parser.error(f"джерело не знайдено: {args.source}")
    try:
        movies, ratings, comments = read_source(args.source)
    except (sqlite3.Error, ValueError) as error:
        parser.error(f"не вдалося прочитати джерело: {error}")

    watched = sum(_source_value(movie, "status") == "watched" for movie in movies)
    print(f"Джерело read-only: {os.path.abspath(args.source)}")
    print(f"  movies={len(movies)}, watched={watched}, ratings={len(ratings)}, comments={len(comments)}")
    if args.dry_run:
        print("DRY-RUN: запису не буде.")
        print(f"  users: {len(USERS)}; films: до {len(movies)}; user_films: до {len(movies) * len(USERS)}")
        return

    is_postgres = db_runtime.uses_postgres(db.DATABASE_URL)
    if is_postgres and not args.allow_postgres:
        parser.error("для PostgreSQL додайте --allow-postgres після перевірки --dry-run")
    if not is_postgres:
        backup = asyncio.run(prepare_sqlite_backup())
        if not backup:
            parser.error("не створено SQLite-бекап — імпорт скасовано")
        print(f"SQLite-бекап: {backup}")
    else:
        print("Приймач: PostgreSQL/Neon (увімкнено явним --allow-postgres; наявні записи не перезаписуються)")

    try:
        new, totals = asyncio.run(import_records(movies, ratings, comments))
    finally:
        asyncio.run(db_runtime.close())
    print(f"Додано: {new}")
    print(f"Усього у приймачі: {totals}")


if __name__ == "__main__":
    main()
