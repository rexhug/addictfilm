"""SQLite-слой (перенесён из movie_bot, без chat-специфики user_messages).

Проверенные решения: WAL, VACUUM INTO-бэкапы, батч-оценки, честная статистика.
"""
import os
import re
import aiosqlite
from datetime import datetime, timezone, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "movies.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL: чтение не блокирует запись — двое жмут кнопки одновременно без locked.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                imdb_id        TEXT UNIQUE NOT NULL,
                title          TEXT NOT NULL,
                title_original TEXT,
                year           TEXT,
                genres         TEXT,
                directors      TEXT,
                actors         TEXT,
                runtime        TEXT,
                imdb_rating    TEXT,
                kp_rating      TEXT,
                imdb_votes     TEXT,
                plot           TEXT,
                poster_url     TEXT,
                status         TEXT DEFAULT 'want_to_watch',
                added_by       INTEGER,
                added_at       TEXT,
                watched_at     TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                movie_id  INTEGER NOT NULL,
                user_id   INTEGER NOT NULL,
                rating    INTEGER NOT NULL,
                rated_at  TEXT,
                UNIQUE(movie_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                movie_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                text       TEXT NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (movie_id, user_id)
            )
        """)
        await db.commit()


async def backup_db(keep: int = 7) -> str | None:
    """Консистентный бэкап (VACUUM INTO) рядом с базой; храним последние `keep`."""
    import glob
    dirname = os.path.dirname(os.path.abspath(DB_PATH))
    path = os.path.join(dirname, f"movies.backup-{datetime.now(timezone.utc):%Y%m%d}.db")
    if not os.path.exists(path):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("VACUUM INTO ?", (path,))
        except Exception:
            return None
    for old in sorted(glob.glob(os.path.join(dirname, "movies.backup-*.db")))[:-keep]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


async def add_movie(
    imdb_id: str, title: str, year: str | None, genres: str | None,
    runtime: str | None, imdb_rating: str | None, imdb_votes: str | None,
    plot: str | None, poster_url: str | None, added_by: int,
    status: str = "want_to_watch", watched_at: str | None = None,
    title_original: str | None = None, kp_rating: str | None = None,
    directors: str | None = None, actors: str | None = None,
) -> int | None:
    """None = такой imdb_id уже есть (дедупликация)."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cursor = await db.execute(
                """
                INSERT INTO movies
                    (imdb_id, title, title_original, year, genres, directors, actors, runtime,
                     imdb_rating, kp_rating, imdb_votes, plot, poster_url, added_by, added_at,
                     status, watched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (imdb_id, title, title_original, year, genres, directors, actors, runtime,
                 imdb_rating, kp_rating, imdb_votes, plot, poster_url, added_by, _now(),
                 status, watched_at),
            )
            await db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            return None


async def get_movie(movie_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM movies WHERE id = ?", (movie_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_movie_by_imdb(imdb_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM movies WHERE imdb_id = ?", (imdb_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def mark_watched(movie_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE movies SET status='watched', watched_at=? WHERE id=?", (_now(), movie_id),
        )
        await db.commit()


async def unmark_watched(movie_id: int) -> None:
    """Вернуть в «Хочу». Оценки сохраняются."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE movies SET status='want_to_watch', watched_at=NULL WHERE id=?", (movie_id,),
        )
        await db.commit()


async def delete_movie(movie_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM ratings WHERE movie_id=?", (movie_id,))
        await db.execute("DELETE FROM comments WHERE movie_id=?", (movie_id,))
        await db.execute("DELETE FROM movies WHERE id=?", (movie_id,))
        await db.commit()


async def set_rating(movie_id: int, user_id: int, rating: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO ratings (movie_id, user_id, rating, rated_at) VALUES (?,?,?,?)
            ON CONFLICT(movie_id, user_id) DO UPDATE SET rating=excluded.rating, rated_at=excluded.rated_at
            """,
            (movie_id, user_id, rating, _now()),
        )
        await db.commit()


async def get_ratings(movie_id: int) -> dict[int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT user_id, rating FROM ratings WHERE movie_id=?", (movie_id,))
        return {row["user_id"]: row["rating"] for row in await cursor.fetchall()}


async def get_ratings_bulk(movie_ids: list[int]) -> dict[int, dict[int, int]]:
    """Оценки по списку фильмов одним запросом (лечит N+1 в списках)."""
    if not movie_ids:
        return {}
    placeholders = ",".join("?" * len(movie_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT movie_id, user_id, rating FROM ratings WHERE movie_id IN ({placeholders})",
            movie_ids,
        )
        out: dict[int, dict[int, int]] = {}
        for row in await cursor.fetchall():
            out.setdefault(row["movie_id"], {})[row["user_id"]] = row["rating"]
        return out


async def set_comment(movie_id: int, user_id: int, text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO comments (movie_id, user_id, text, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(movie_id, user_id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at
            """,
            (movie_id, user_id, text, _now()),
        )
        await db.commit()


async def delete_comment(movie_id: int, user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM comments WHERE movie_id=? AND user_id=?", (movie_id, user_id))
        await db.commit()


async def get_comments(movie_id: int) -> dict[int, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT user_id, text FROM comments WHERE movie_id=?", (movie_id,))
        return {row["user_id"]: row["text"] for row in await cursor.fetchall()}


async def get_movies_by_status(status: str, limit: int = 20, offset: int = 0, sort: str = "date") -> list[dict]:
    """status: want_to_watch | watched | top (top = оба оценили, по средней)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status == "top":
            cursor = await db.execute(
                """
                SELECT m.*, AVG(r.rating) AS avg_rating FROM movies m
                JOIN ratings r ON m.id = r.movie_id
                WHERE m.status = 'watched' GROUP BY m.id HAVING COUNT(r.id) >= 2
                ORDER BY avg_rating DESC LIMIT ? OFFSET ?
                """, (limit, offset))
        elif status == "watched":
            if sort == "rating":
                cursor = await db.execute(
                    """
                    SELECT m.*, COALESCE(AVG(r.rating), 0) AS avg_rating FROM movies m
                    LEFT JOIN ratings r ON m.id = r.movie_id
                    WHERE m.status = 'watched' GROUP BY m.id
                    ORDER BY avg_rating DESC LIMIT ? OFFSET ?
                    """, (limit, offset))
            else:
                cursor = await db.execute(
                    "SELECT *, NULL AS avg_rating FROM movies WHERE status='watched' "
                    "ORDER BY watched_at DESC LIMIT ? OFFSET ?", (limit, offset))
        else:
            cursor = await db.execute(
                "SELECT *, NULL AS avg_rating FROM movies WHERE status='want_to_watch' "
                "ORDER BY added_at DESC LIMIT ? OFFSET ?", (limit, offset))
        return [dict(r) for r in await cursor.fetchall()]


async def count_movies(status: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        if status == "top":
            cursor = await db.execute(
                "SELECT COUNT(*) FROM (SELECT m.id FROM movies m JOIN ratings r ON m.id=r.movie_id "
                "WHERE m.status='watched' GROUP BY m.id HAVING COUNT(r.id) >= 2)")
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM movies WHERE status=?", (status,))
        return (await cursor.fetchone())[0]


async def get_random_want() -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM movies WHERE status='want_to_watch' ORDER BY RANDOM() LIMIT 1")
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_unrated_watched(user_id: int, since_days: int = 30, limit: int = 10) -> list[dict]:
    """Просмотренные за N дней, не оценённые пользователем (для напоминаний ботом)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, title FROM movies
            WHERE status = 'watched' AND (watched_at IS NULL OR watched_at >= ?)
              AND id NOT IN (SELECT movie_id FROM ratings WHERE user_id = ?)
            ORDER BY watched_at DESC LIMIT ?
            """, (cutoff, user_id, limit))
        return [dict(r) for r in await cursor.fetchall()]


async def get_stats(user1_id: int, user2_id: int) -> dict:
    """Общая статистика пары: счётчики, экранное время, средние, жанры,
    актёры/режиссёры (ничьи честно), совпадения вкусов, самый спорный."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        watched = (await (await db.execute(
            "SELECT COUNT(*) c FROM movies WHERE status='watched'")).fetchone())["c"]
        want = (await (await db.execute(
            "SELECT COUNT(*) c FROM movies WHERE status='want_to_watch'")).fetchone())["c"]

        avg_ratings, rating_counts = {}, {}
        cursor = await db.execute(
            "SELECT user_id, AVG(rating) avg, COUNT(*) cnt FROM ratings GROUP BY user_id")
        for row in await cursor.fetchall():
            avg_ratings[row["user_id"]] = round(row["avg"], 1)
            rating_counts[row["user_id"]] = row["cnt"]

        cursor = await db.execute(
            "SELECT genres, actors, directors, runtime FROM movies WHERE status='watched'")
        genre_counts, actor_counts, director_counts = {}, {}, {}
        total_runtime_min = 0
        for row in await cursor.fetchall():
            for g in (row["genres"] or "").split(","):
                g = g.strip()
                if g and g != "N/A":
                    genre_counts[g] = genre_counts.get(g, 0) + 1
            for a in (row["actors"] or "").split(","):
                a = a.strip()
                if a:
                    actor_counts[a] = actor_counts.get(a, 0) + 1
            for d in (row["directors"] or "").split(","):
                d = d.strip()
                if d:
                    director_counts[d] = director_counts.get(d, 0) + 1
            m = re.search(r"\d+", row["runtime"] or "")
            if m:
                total_runtime_min += int(m.group(0))

        total_refs = sum(genre_counts.values())
        top_genres_pct = [
            (g, round(c / total_refs * 100))
            for g, c in sorted(genre_counts.items(), key=lambda x: -x[1])[:5]
        ] if total_refs else []
        top_actors = [(n, c) for n, c in sorted(actor_counts.items(), key=lambda x: -x[1]) if c >= 2][:5]
        top_directors = [(n, c) for n, c in sorted(director_counts.items(), key=lambda x: -x[1]) if c >= 2][:3]

        # Точные совпадения оценок.
        cursor = await db.execute(
            """
            SELECT m.title AS title, r1.rating AS r FROM ratings r1
            JOIN ratings r2 ON r1.movie_id = r2.movie_id AND r1.rating = r2.rating
            JOIN movies m ON m.id = r1.movie_id
            WHERE r1.user_id = ? AND r2.user_id = ? ORDER BY r DESC
            """, (user1_id, user2_id))
        match_rows = await cursor.fetchall()

        # Самый спорный.
        cursor = await db.execute(
            """
            SELECT m.title, r1.rating AS r1, r2.rating AS r2, ABS(r1.rating - r2.rating) AS diff
            FROM movies m
            JOIN ratings r1 ON m.id = r1.movie_id AND r1.user_id = ?
            JOIN ratings r2 ON m.id = r2.movie_id AND r2.user_id = ?
            WHERE m.status = 'watched' ORDER BY diff DESC LIMIT 1
            """, (user1_id, user2_id))
        row = await cursor.fetchone()
        controversial = dict(row) if row else None

        cursor = await db.execute(
            "SELECT title FROM movies WHERE status='watched' ORDER BY watched_at DESC LIMIT 1")
        row = await cursor.fetchone()

        return {
            "watched": watched,
            "want": want,
            "avg_ratings": avg_ratings,
            "rating_counts": rating_counts,
            "total_runtime_min": total_runtime_min,
            "top_genres_pct": top_genres_pct,
            "top_actors": top_actors,
            "top_directors": top_directors,
            "taste_match_count": len(match_rows),
            "perfect_match": dict(match_rows[0]) if match_rows else None,
            "controversial": controversial,
            "last_watched": row["title"] if row else None,
        }


async def get_compatibility(user1_id: int, user2_id: int) -> dict:
    """Совместимость вкусов: 100 - mean(|r1-r2|)/9*100 по общим фильмам."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT AVG(ABS(r1.rating - r2.rating)) AS avg_diff, COUNT(*) AS cnt
            FROM ratings r1 JOIN ratings r2 ON r1.movie_id = r2.movie_id
            WHERE r1.user_id = ? AND r2.user_id = ?
            """, (user1_id, user2_id))
        row = await cursor.fetchone()
        avg_diff, count = row[0], row[1] or 0
        if not count or avg_diff is None:
            return {"count": 0, "agreement": None}
        return {"count": count, "agreement": round(100 - (avg_diff / 9) * 100)}


async def get_year_stats(year: int) -> dict:
    """Итоги года. Ничьи — честно: список лучших, «актёр года» только при
    единоличном лидерстве (урок: случайный «первый из пяти» — это ложь)."""
    like = f"{year}-%"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT runtime, genres, actors FROM movies WHERE status='watched' AND watched_at LIKE ?",
            (like,))
        rows = await cursor.fetchall()

        total_min, genre_counts, actor_counts = 0, {}, {}
        for r in rows:
            m = re.search(r"\d+", r["runtime"] or "")
            if m:
                total_min += int(m.group(0))
            for g in (r["genres"] or "").split(","):
                g = g.strip()
                if g and g != "N/A":
                    genre_counts[g] = genre_counts.get(g, 0) + 1
            for a in (r["actors"] or "").split(","):
                a = a.strip()
                if a:
                    actor_counts[a] = actor_counts.get(a, 0) + 1

        top_genre = max(genre_counts.items(), key=lambda x: x[1])[0] if genre_counts else None
        ranked = sorted(actor_counts.items(), key=lambda x: -x[1])
        top_actor = None
        if ranked and ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            top_actor = ranked[0]

        cursor = await db.execute(
            "SELECT AVG(r.rating) AS avg FROM ratings r JOIN movies m ON m.id = r.movie_id "
            "WHERE m.status='watched' AND m.watched_at LIKE ?", (like,))
        row = await cursor.fetchone()
        avg_rating = round(row["avg"], 1) if row and row["avg"] is not None else None

        cursor = await db.execute(
            """
            SELECT m.title, AVG(r.rating) AS avg FROM movies m JOIN ratings r ON m.id = r.movie_id
            WHERE m.status='watched' AND m.watched_at LIKE ?
            GROUP BY m.id HAVING COUNT(r.id) >= 2 ORDER BY avg DESC
            """, (like,))
        rated = await cursor.fetchall()
        best_avg = rated[0]["avg"] if rated else None
        best_titles = [r["title"] for r in rated if r["avg"] == best_avg] if rated else []

        return {
            "year": year,
            "count": len(rows),
            "total_runtime_min": total_min,
            "top_genre": top_genre,
            "top_actor": top_actor,
            "avg_rating": avg_rating,
            "best_avg": round(best_avg, 1) if best_avg is not None else None,
            "best_titles": best_titles,
        }
