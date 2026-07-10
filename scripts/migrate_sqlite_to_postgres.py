"""Идемпотентно переносит рабочую SQLite-базу Mini App в PostgreSQL.

Источник открывается строго в read-only режиме. Скрипт не удаляет и не
перезаписывает существующие записи в PostgreSQL, поэтому его можно безопасно
повторить после сетевого сбоя. Запускать только с DATABASE_URL PostgreSQL.
"""
import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import database as target_db
from config import DATABASE_URL
import db_runtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Переносит movies.db в PostgreSQL")
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT / "movies.db")
    parser.add_argument("--dry-run", action="store_true", help="Показать состав источника без записи")
    return parser.parse_args()


def _read_table(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return []
    return connection.execute(f"SELECT * FROM {table}").fetchall()


def read_source(path: Path) -> dict[str, list[sqlite3.Row]]:
    if not path.is_file():
        raise FileNotFoundError(f"Не найдена SQLite-база: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return {
            name: _read_table(connection, name)
            for name in ("users", "movies", "user_movies", "ratings", "comments", "partnerships", "partner_invites")
        }
    finally:
        connection.close()


async def migrate(data: dict[str, list[sqlite3.Row]]) -> dict[str, int]:
    await target_db.init_db()
    counts = {"users": 0, "movies": 0, "user_movies": 0, "ratings": 0, "comments": 0, "partnerships": 0, "invites": 0}

    for source_user in data["users"]:
        if await target_db.import_user_record(dict(source_user)):
            counts["users"] += 1

    movie_ids: dict[int, int] = {}
    for source_movie in data["movies"]:
        movie = dict(source_movie)
        existed = await target_db.get_movie_by_imdb(movie["imdb_id"])
        movie_ids[movie["id"]] = await target_db.get_or_create_movie(
            imdb_id=movie["imdb_id"],
            title=movie["title"],
            title_original=movie.get("title_original"),
            year=movie.get("year"),
            genres=movie.get("genres"),
            directors=movie.get("directors"),
            actors=movie.get("actors"),
            runtime=movie.get("runtime"),
            imdb_rating=movie.get("imdb_rating"),
            kp_rating=movie.get("kp_rating"),
            imdb_votes=movie.get("imdb_votes"),
            plot=movie.get("plot"),
            poster_url=movie.get("poster_url"),
        )
        if existed is None:
            counts["movies"] += 1

    for item in data["user_movies"]:
        record = dict(item)
        movie_id = movie_ids.get(record["movie_id"])
        if movie_id is None:
            continue
        if await target_db.import_user_movie(
            record["user_id"], movie_id, record["status"], record.get("added_at"), record.get("watched_at"),
        ):
            counts["user_movies"] += 1

    for rating in data["ratings"]:
        record = dict(rating)
        movie_id = movie_ids.get(record["movie_id"])
        if movie_id is not None and await target_db.import_rating(
            movie_id, record["user_id"], record["rating"], record.get("rated_at"),
        ):
            counts["ratings"] += 1

    for comment in data["comments"]:
        record = dict(comment)
        movie_id = movie_ids.get(record["movie_id"])
        text = (record.get("text") or "").strip()
        if movie_id is not None and text and await target_db.import_comment(
            movie_id, record["user_id"], text, record.get("updated_at"),
        ):
            counts["comments"] += 1

    for partnership in data["partnerships"]:
        record = dict(partnership)
        if await target_db.ensure_imported_partnership(record["user1_id"], record["user2_id"]) == "created":
            counts["partnerships"] += 1

    for invite in data["partner_invites"]:
        if await target_db.import_partner_invite(dict(invite)):
            counts["invites"] += 1
    return counts


async def main() -> None:
    args = parse_args()
    data = read_source(args.source.expanduser().resolve())
    source_counts = {name: len(rows) for name, rows in data.items()}
    if args.dry_run:
        print({"source": source_counts})
        return
    if not db_runtime.uses_postgres(DATABASE_URL):
        raise RuntimeError("Передайте DATABASE_URL PostgreSQL через окружение или .env")
    try:
        print({"source": source_counts, "inserted": await migrate(data)})
    finally:
        await target_db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
