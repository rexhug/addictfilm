#!/usr/bin/env python3
"""Импорт данных старого movie_bot в схему Mini App (users / films / user_films).

- Источник (старый бот) открывается ТОЛЬКО read-only, ничего в нём не меняется.
- Приёмник — база апки (backend.database.DB_PATH; в облаке DB_PATH=/data/movies.db).
- Идемпотентно: повторный запуск ничего не дублирует (ON CONFLICT DO NOTHING),
  «новых» будет 0. Перед записью делает бэкап базы апки (VACUUM INTO).
- Маппинг: старый общий список «на двоих» → per-user записи user_films для ОБОИХ
  пользователей (каждый фильм попадает в списки обоих). Оценки/комментарии — по юзеру.
  Сущности «партнёрство» в новой схеме нет — не создаём.

Использование:
  python3 scripts/import_movie_bot.py --dry-run     # только показать, что будет
  python3 scripts/import_movie_bot.py               # реальный импорт (с бэкапом)
"""
import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import database as db  # noqa: E402

SRC_DEFAULT = "/Users/denyszapriahailo/movie_bot/movies.db"
# Два пользователя старого бота (из movie_bot/config.py: USER1=Денис, USER2=Котятко).
USERS = {1001453723: "Денис", 5310882391: "Котятко"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_source(path: str):
    """Читает старого бота read-only: (movies, ratings, comments)."""
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    movies = src.execute("SELECT * FROM movies").fetchall()
    ratings = {(r["user_id"], r["movie_id"]): r["rating"]
               for r in src.execute("SELECT movie_id, user_id, rating FROM ratings")}
    comments = {(r["user_id"], r["movie_id"]): r["text"]
                for r in src.execute("SELECT movie_id, user_id, text FROM comments")}
    src.close()
    return movies, ratings, comments


def main():
    ap = argparse.ArgumentParser(description="Импорт movie_bot → Mini App")
    ap.add_argument("--dry-run", action="store_true", help="только показать, ничего не писать")
    ap.add_argument("--source", default=SRC_DEFAULT, help="путь к movies.db старого бота")
    args = ap.parse_args()

    if not os.path.exists(args.source):
        print(f"ОШИБКА: источник не найден: {args.source}", file=sys.stderr)
        sys.exit(1)

    movies, ratings, comments = read_source(args.source)
    watched = sum(1 for m in movies if m["status"] == "watched")
    print(f"Источник (read-only): {args.source}")
    print(f"  {{'movies': {len(movies)}, 'watched': {watched}, "
          f"'ratings': {len(ratings)}, 'comments': {len(comments)}}}")

    if args.dry_run:
        print("\nDRY-RUN — ничего не пишем. Будет импортировано в схему апки:")
        print(f"  users:      {len(USERS)}  (Денис, Котятко)")
        print(f"  films:      до {len(movies)}  (dedup по imdb_id)")
        print(f"  user_films: до {len(movies) * len(USERS)}  (каждый фильм в списках обоих)")
        print("  партнёрство: — (в новой схеме такой сущности нет, пропускается)")
        return

    # Приёмник: схема + бэкап.
    print(f"\nПриёмник: {os.path.abspath(db.DB_PATH)}")
    asyncio.run(db.init_db())
    backup = asyncio.run(db.backup_db())
    print(f"Бэкап базы апки: {backup or '(не удался — прерываю)'}")
    if not backup:
        sys.exit(1)

    con = sqlite3.connect(db.DB_PATH)
    con.row_factory = sqlite3.Row
    now = _now()
    new = {"users": 0, "films": 0, "user_films": 0}

    for uid, name in USERS.items():
        cur = con.execute(
            "INSERT INTO users (id, first_name, username, created_at, last_seen) "
            "VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (uid, name, None, now, now))
        new["users"] += cur.rowcount

    film_of_old = {}
    for m in movies:
        imdb = m["imdb_id"] or f"mb_{m['id']}"
        row = con.execute("SELECT id FROM films WHERE imdb_id = ?", (imdb,)).fetchone()
        if row:
            fid = row["id"]
        else:
            cur = con.execute(
                "INSERT INTO films (imdb_id, title, title_original, year, genres, directors, "
                "actors, runtime, imdb_rating, kp_rating, imdb_votes, plot, poster_url, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (imdb, m["title"], m["title_original"], m["year"], m["genres"], m["directors"],
                 m["actors"], m["runtime"], m["imdb_rating"], m["kp_rating"], m["imdb_votes"],
                 m["plot"], m["poster_url"], now))
            fid = cur.lastrowid
            new["films"] += 1
        film_of_old[m["id"]] = (fid, m)

    for old_id, (fid, m) in film_of_old.items():
        status = m["status"] if m["status"] in ("watched", "want_to_watch") else "want_to_watch"
        watched_at = m["watched_at"] if status == "watched" else None
        for uid in USERS:
            rating = ratings.get((uid, old_id))
            comment = comments.get((uid, old_id))
            cur = con.execute(
                "INSERT INTO user_films (user_id, film_id, status, rating, comment, added_at, "
                "watched_at, rated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id, film_id) DO NOTHING",
                (uid, fid, status, rating, comment, m["added_at"] or now, watched_at,
                 now if rating is not None else None))
            new["user_films"] += cur.rowcount

    con.commit()
    totals = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("users", "films", "user_films")}
    con.close()

    print(f"\nИмпортировано НОВОГО: {new}")
    print(f"Итого в базе апки:   {totals}")
    print("(повторный запуск даст 0 нового — данные уже перенесены)")


if __name__ == "__main__":
    main()
