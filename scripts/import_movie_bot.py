"""Одноразовый перенос истории из старого movie_bot в Movie Mini App.

Источник читается только в режиме SQLite read-only. Каждый старый фильм был
общим для пары, поэтому он добавляется в личные списки обоих пользователей;
оценки и комментарии остаются личными. Повторный запуск безопасен: уже
импортированные данные не перезаписывают новые изменения в Mini App.
"""
import argparse
import asyncio
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import database as target_db

DEFAULT_SOURCE_ROOT = Path("/Users/denyszapriahailo/movie_bot")
USER_ID_PATTERN = re.compile(r"^\s*(USER[12]_ID)\s*=\s*(\d+)\s*(?:#.*)?$")
MOVIE_COLUMNS = """
    id, imdb_id, title, title_original, year, genres, directors, actors, runtime,
    imdb_rating, kp_rating, imdb_votes, plot, poster_url, status, added_at, watched_at
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Переносит историю из старого movie_bot")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--target-db", type=Path, default=PROJECT_ROOT / "movies.db")
    parser.add_argument("--dry-run", action="store_true", help="Только показать состав источника")
    return parser.parse_args()


def read_legacy_user_ids(env_path: Path) -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = USER_ID_PATTERN.match(line)
        if match:
            values[match.group(1)] = int(match.group(2))
    user1_id = values.get("USER1_ID")
    user2_id = values.get("USER2_ID")
    if not user1_id or not user2_id or user1_id == user2_id:
        raise ValueError("В .env старого бота не найдены два разных USER1_ID и USER2_ID")
    return user1_id, user2_id


def read_legacy_data(source_db_path: Path) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
    if not source_db_path.is_file():
        raise FileNotFoundError(f"Не найдена БД старого бота: {source_db_path}")
    connection = sqlite3.connect(f"{source_db_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        movies = connection.execute(f"SELECT {MOVIE_COLUMNS} FROM movies ORDER BY id").fetchall()
        ratings = connection.execute("SELECT movie_id, user_id, rating, rated_at FROM ratings").fetchall()
        comments = connection.execute("SELECT movie_id, user_id, text, updated_at FROM comments").fetchall()
        return movies, ratings, comments
    finally:
        connection.close()


async def import_data(
    movies: list[sqlite3.Row], ratings: list[sqlite3.Row], comments: list[sqlite3.Row], user_ids: tuple[int, int],
) -> dict:
    user1_id, user2_id = user_ids
    await target_db.init_db()
    backup_path = await target_db.backup_db(force=True)
    if not backup_path:
        raise RuntimeError("Не удалось создать резервную копию Mini App перед импортом")

    # Имена наследуются из прежнего бота и будут уточнены при первом входе в Telegram.
    await target_db.ensure_imported_user(user1_id, "Денис")
    await target_db.ensure_imported_user(user2_id, "Котятко")

    ratings_by_movie: dict[int, list[sqlite3.Row]] = defaultdict(list)
    comments_by_movie: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for rating in ratings:
        ratings_by_movie[rating["movie_id"]].append(rating)
    for comment in comments:
        comments_by_movie[comment["movie_id"]].append(comment)

    imported_lists = imported_ratings = imported_comments = 0
    allowed_users = {user1_id, user2_id}
    for legacy_movie in movies:
        movie_id = await target_db.get_or_create_movie(
            imdb_id=legacy_movie["imdb_id"],
            title=legacy_movie["title"],
            title_original=legacy_movie["title_original"],
            year=legacy_movie["year"],
            genres=legacy_movie["genres"],
            directors=legacy_movie["directors"],
            actors=legacy_movie["actors"],
            runtime=legacy_movie["runtime"],
            imdb_rating=legacy_movie["imdb_rating"],
            kp_rating=legacy_movie["kp_rating"],
            imdb_votes=legacy_movie["imdb_votes"],
            plot=legacy_movie["plot"],
            poster_url=legacy_movie["poster_url"],
        )
        for user_id in user_ids:
            if await target_db.import_user_movie(
                user_id, movie_id, legacy_movie["status"], legacy_movie["added_at"], legacy_movie["watched_at"],
            ):
                imported_lists += 1
        for rating in ratings_by_movie[legacy_movie["id"]]:
            if rating["user_id"] in allowed_users and 1 <= rating["rating"] <= 10:
                if await target_db.import_rating(movie_id, rating["user_id"], rating["rating"], rating["rated_at"]):
                    imported_ratings += 1
        for comment in comments_by_movie[legacy_movie["id"]]:
            if comment["user_id"] in allowed_users and comment["text"].strip():
                if await target_db.import_comment(movie_id, comment["user_id"], comment["text"], comment["updated_at"]):
                    imported_comments += 1

    partnership = await target_db.ensure_imported_partnership(user1_id, user2_id)
    return {
        "backup": str(backup_path),
        "movies": len(movies),
        "personal_lists": imported_lists,
        "ratings": imported_ratings,
        "comments": imported_comments,
        "partnership": partnership,
    }


async def main() -> None:
    args = parse_args()
    source_root = args.source_root.expanduser().resolve()
    user_ids = read_legacy_user_ids(source_root / ".env")
    movies, ratings, comments = read_legacy_data(source_root / "movies.db")
    if args.dry_run:
        watched = sum(movie["status"] == "watched" for movie in movies)
        print({"movies": len(movies), "watched": watched, "ratings": len(ratings), "comments": len(comments)})
        return

    target_db.DB_PATH = str(args.target_db.expanduser().resolve())
    summary = await import_data(movies, ratings, comments, user_ids)
    print(summary)


if __name__ == "__main__":
    asyncio.run(main())
