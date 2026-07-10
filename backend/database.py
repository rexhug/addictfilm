"""SQLite-слой публичного кинотрекера.

Каталог фильмов общий, а списки, оценки и комментарии принадлежат конкретному
пользователю. Пара — необязательная связь поверх личных данных.
"""
import glob
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import aiosqlite

import db_runtime
from config import DATABASE_URL

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "movies.db")


@asynccontextmanager
async def _connect() -> AsyncIterator[Any]:
    """Открывает SQLite локально или соединение из пула PostgreSQL в продакшене."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as connection:
        yield connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _movie_dict(row: aiosqlite.Row) -> dict:
    """Приводит строку пользовательского списка к формату API фильма."""
    movie = dict(row)
    movie["status"] = movie.pop("user_status")
    movie["added_at"] = movie.pop("user_added_at")
    movie["watched_at"] = movie.pop("user_watched_at")
    return movie


async def _init_postgres_schema(db: Any) -> None:
    """Создаёт схему PostgreSQL для публичного сервиса с явными ограничениями."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS movies (
            id BIGSERIAL PRIMARY KEY,
            imdb_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            title_original TEXT,
            year TEXT,
            genres TEXT,
            directors TEXT,
            actors TEXT,
            runtime TEXT,
            imdb_rating TEXT,
            kp_rating TEXT,
            imdb_votes TEXT,
            plot TEXT,
            poster_url TEXT,
            status TEXT DEFAULT 'want_to_watch',
            added_by BIGINT,
            added_at TEXT,
            watched_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_movies (
            user_id BIGINT NOT NULL REFERENCES users(id),
            movie_id BIGINT NOT NULL REFERENCES movies(id),
            status TEXT NOT NULL DEFAULT 'want_to_watch'
                   CHECK(status IN ('want_to_watch', 'watched')),
            added_at TEXT NOT NULL,
            watched_at TEXT,
            PRIMARY KEY (user_id, movie_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ratings (
            id BIGSERIAL PRIMARY KEY,
            movie_id BIGINT NOT NULL REFERENCES movies(id),
            user_id BIGINT NOT NULL REFERENCES users(id),
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 10),
            rated_at TEXT,
            UNIQUE(movie_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS comments (
            movie_id BIGINT NOT NULL REFERENCES movies(id),
            user_id BIGINT NOT NULL REFERENCES users(id),
            text TEXT NOT NULL CHECK(char_length(text) <= 500),
            updated_at TEXT,
            PRIMARY KEY (movie_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS partnerships (
            id BIGSERIAL PRIMARY KEY,
            user1_id BIGINT NOT NULL REFERENCES users(id),
            user2_id BIGINT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            CHECK(user1_id < user2_id),
            UNIQUE(user1_id, user2_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS partner_invites (
            token TEXT PRIMARY KEY,
            inviter_id BIGINT NOT NULL REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending', 'accepted', 'cancelled', 'expired')),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            accepted_by BIGINT REFERENCES users(id),
            accepted_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_user_movies_list ON user_movies(user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_ratings_movie_user ON ratings(movie_id, user_id)",
        "CREATE INDEX IF NOT EXISTS idx_partnerships_user1 ON partnerships(user1_id)",
        "CREATE INDEX IF NOT EXISTS idx_partnerships_user2 ON partnerships(user2_id)",
    )
    for statement in statements:
        await db.execute(statement)


async def init_db() -> None:
    """Создаёт схему, совместимую с прежней тестовой базой без удаления данных."""
    await db_runtime.start(DATABASE_URL)
    async with _connect() as db:
        if db_runtime.uses_postgres(DATABASE_URL):
            await _init_postgres_schema(db)
            return
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                last_name     TEXT,
                created_at    TEXT NOT NULL,
                last_seen_at  TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_movies (
                user_id     INTEGER NOT NULL,
                movie_id    INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'want_to_watch'
                            CHECK(status IN ('want_to_watch', 'watched')),
                added_at    TEXT NOT NULL,
                watched_at  TEXT,
                PRIMARY KEY (user_id, movie_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (movie_id) REFERENCES movies(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS partnerships (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user1_id    INTEGER NOT NULL,
                user2_id    INTEGER NOT NULL,
                created_at  TEXT NOT NULL,
                CHECK(user1_id < user2_id),
                UNIQUE(user1_id, user2_id),
                FOREIGN KEY (user1_id) REFERENCES users(id),
                FOREIGN KEY (user2_id) REFERENCES users(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS partner_invites (
                token        TEXT PRIMARY KEY,
                inviter_id   INTEGER NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending'
                             CHECK(status IN ('pending', 'accepted', 'cancelled', 'expired')),
                created_at   TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                accepted_by  INTEGER,
                accepted_at  TEXT,
                FOREIGN KEY (inviter_id) REFERENCES users(id),
                FOREIGN KEY (accepted_by) REFERENCES users(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_movies_list ON user_movies(user_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ratings_movie_user ON ratings(movie_id, user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_partnerships_user1 ON partnerships(user1_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_partnerships_user2 ON partnerships(user2_id)")
        await db.commit()


async def backup_db(keep: int = 7, force: bool = False) -> str | None:
    """Делает локальный SQLite-бэкап; PostgreSQL резервируется хостингом."""
    if db_runtime.uses_postgres(DATABASE_URL):
        return None
    dirname = os.path.dirname(os.path.abspath(DB_PATH))
    timestamp = datetime.now(timezone.utc)
    suffix = timestamp.strftime("%Y%m%d-%H%M%S") if force else timestamp.strftime("%Y%m%d")
    path = os.path.join(dirname, f"movies.backup-{suffix}.db")
    if not os.path.exists(path):
        try:
            async with _connect() as db:
                await db.execute("VACUUM INTO ?", (path,))
        except Exception:
            return None
    for old in sorted(glob.glob(os.path.join(dirname, "movies.backup-*.db")))[:-keep]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


async def upsert_user(telegram_user: dict) -> dict:
    """Создаёт профиль при первом входе и обновляет публичные данные Telegram."""
    user_id = telegram_user["id"]
    now = _now()
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""
            INSERT INTO users (id, username, first_name, last_name, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                last_seen_at=excluded.last_seen_at
        """, (
            user_id,
            telegram_user.get("username"),
            telegram_user.get("first_name"),
            telegram_user.get("last_name"),
            now,
            now,
        ))
        await db.commit()
        cursor = await db.execute("SELECT * FROM users WHERE id=?", (user_id,))
        return dict(await cursor.fetchone())


async def get_user(user_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def ensure_imported_user(user_id: int, first_name: str) -> None:
    """Создаёт профиль для истории из старого бота, не перезаписывая профиль Telegram."""
    now = _now()
    async with _connect() as db:
        await db.execute("""
            INSERT INTO users (id, first_name, created_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
        """, (user_id, first_name, now, now))
        await db.commit()


async def import_user_record(user: dict) -> bool:
    """Переносит профиль из SQLite, не перезаписывая более свежий Telegram-профиль."""
    async with _connect() as db:
        cursor = await db.execute("""
            INSERT INTO users (id, username, first_name, last_name, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
        """, (
            user["id"],
            user.get("username"),
            user.get("first_name"),
            user.get("last_name"),
            user.get("created_at") or _now(),
            user.get("last_seen_at") or _now(),
        ))
        await db.commit()
        return cursor.rowcount == 1


async def get_partner(user_id: int) -> dict | None:
    """Возвращает единственного подключённого партнёра пользователя, если он есть."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT u.* FROM partnerships p
            JOIN users u ON u.id = CASE WHEN p.user1_id = ? THEN p.user2_id ELSE p.user1_id END
            WHERE p.user1_id = ? OR p.user2_id = ?
            LIMIT 1
        """, (user_id, user_id, user_id))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _has_partner(db: Any, user_id: int) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM partnerships WHERE user1_id=? OR user2_id=? LIMIT 1",
        (user_id, user_id),
    )
    return await cursor.fetchone() is not None


async def _lock_partner_users(db: Any, *user_ids: int) -> None:
    """Сериализует изменения пары в PostgreSQL и предотвращает гонку инвайтов."""
    if not db_runtime.uses_postgres(DATABASE_URL):
        return
    for user_id in sorted(set(user_ids)):
        await db.execute("SELECT pg_advisory_xact_lock(?)", (user_id,))


async def create_partner_invite(inviter_id: int, token: str, expires_at: str) -> dict | None:
    """Создаёт одноразовое приглашение. У пользователя может быть только один партнёр."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _lock_partner_users(db, inviter_id)
            if await _has_partner(db, inviter_id):
                await db.rollback()
                return None
            await db.execute(
                "UPDATE partner_invites SET status='cancelled' WHERE inviter_id=? AND status='pending'",
                (inviter_id,),
            )
            created_at = _now()
            await db.execute("""
                INSERT INTO partner_invites (token, inviter_id, status, created_at, expires_at)
                VALUES (?, ?, 'pending', ?, ?)
            """, (token, inviter_id, created_at, expires_at))
            await db.commit()
            return {
                "token": token,
                "created_at": created_at,
                "expires_at": expires_at,
            }
        except Exception:
            await db.rollback()
            raise


async def accept_partner_invite(token: str, invitee_id: int) -> tuple[str, dict | None]:
    """Принимает инвайт атомарно и не даёт создать несколько партнёров."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT * FROM partner_invites WHERE token=?", (token,))
            invite = await cursor.fetchone()
            if not invite:
                await db.rollback()
                return "not_found", None
            if invite["status"] != "pending":
                await db.rollback()
                return "already_used", None
            if invite["expires_at"] <= _now():
                await db.execute("UPDATE partner_invites SET status='expired' WHERE token=?", (token,))
                await db.commit()
                return "expired", None
            inviter_id = invite["inviter_id"]
            if inviter_id == invitee_id:
                await db.rollback()
                return "self", None
            await _lock_partner_users(db, inviter_id, invitee_id)
            if await _has_partner(db, inviter_id) or await _has_partner(db, invitee_id):
                await db.rollback()
                return "already_paired", None

            user1_id, user2_id = sorted((inviter_id, invitee_id))
            await db.execute(
                "INSERT INTO partnerships (user1_id, user2_id, created_at) VALUES (?, ?, ?)",
                (user1_id, user2_id, _now()),
            )
            accepted_at = _now()
            await db.execute("""
                UPDATE partner_invites
                SET status='accepted', accepted_by=?, accepted_at=?
                WHERE token=?
            """, (invitee_id, accepted_at, token))
            await db.commit()
            cursor = await db.execute("SELECT * FROM users WHERE id=?", (inviter_id,))
            partner = await cursor.fetchone()
            return "accepted", dict(partner) if partner else None
        except Exception:
            await db.rollback()
            raise


async def remove_partner(user_id: int) -> bool:
    """Отключает пару, не затрагивая личные списки и оценки обоих людей."""
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _lock_partner_users(db, user_id)
            cursor = await db.execute(
                "DELETE FROM partnerships WHERE user1_id=? OR user2_id=?",
                (user_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0
        except Exception:
            await db.rollback()
            raise


async def ensure_imported_partnership(user1_id: int, user2_id: int) -> str:
    """Создаёт пару при импорте, если ни у кого из двоих нет другого партнёра."""
    if user1_id == user2_id:
        return "invalid"
    first_id, second_id = sorted((user1_id, user2_id))
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _lock_partner_users(db, first_id, second_id)
            cursor = await db.execute(
                "SELECT 1 FROM partnerships WHERE user1_id=? AND user2_id=?",
                (first_id, second_id),
            )
            if await cursor.fetchone():
                await db.rollback()
                return "existing"
            if await _has_partner(db, first_id) or await _has_partner(db, second_id):
                await db.rollback()
                return "conflict"
            await db.execute(
                "INSERT INTO partnerships (user1_id, user2_id, created_at) VALUES (?, ?, ?)",
                (first_id, second_id, _now()),
            )
            await db.commit()
            return "created"
        except Exception:
            await db.rollback()
            raise


async def get_or_create_movie(
    imdb_id: str, title: str, year: str | None, genres: str | None,
    runtime: str | None, imdb_rating: str | None, imdb_votes: str | None,
    plot: str | None, poster_url: str | None, title_original: str | None = None,
    kp_rating: str | None = None, directors: str | None = None, actors: str | None = None,
) -> int:
    """Возвращает id фильма из общего каталога, создавая его только при необходимости."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""
            INSERT INTO movies (
                imdb_id, title, title_original, year, genres, directors, actors, runtime,
                imdb_rating, kp_rating, imdb_votes, plot, poster_url
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(imdb_id) DO NOTHING
        """, (
            imdb_id, title, title_original, year, genres, directors, actors, runtime,
            imdb_rating, kp_rating, imdb_votes, plot, poster_url,
        ))
        await db.commit()
        cursor = await db.execute("SELECT id FROM movies WHERE imdb_id=?", (imdb_id,))
        row = await cursor.fetchone()
        if not row:
            raise RuntimeError("Не удалось получить созданный фильм")
        return row["id"]


async def get_movie_by_imdb(imdb_id: str) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM movies WHERE imdb_id=?", (imdb_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def add_user_movie(user_id: int, movie_id: int, status: str = "want_to_watch") -> bool:
    """Добавляет фильм в личный список; False означает, что он уже был в нём."""
    watched_at = _now() if status == "watched" else None
    async with _connect() as db:
        cursor = await db.execute("""
            INSERT INTO user_movies (user_id, movie_id, status, added_at, watched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, movie_id) DO NOTHING
        """, (user_id, movie_id, status, _now(), watched_at))
        await db.commit()
        return cursor.rowcount == 1


async def import_user_movie(
    user_id: int, movie_id: int, status: str, added_at: str | None, watched_at: str | None,
) -> bool:
    """Добавляет запись старого общего списка, сохраняя исходные даты и не перезаписывая новые данные."""
    normalized_status = "watched" if status == "watched" else "want_to_watch"
    async with _connect() as db:
        cursor = await db.execute("""
            INSERT INTO user_movies (user_id, movie_id, status, added_at, watched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, movie_id) DO NOTHING
        """, (user_id, movie_id, normalized_status, added_at or _now(), watched_at))
        await db.commit()
        return cursor.rowcount == 1


async def get_movie(user_id: int, movie_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT m.*, um.status AS user_status, um.added_at AS user_added_at,
                   um.watched_at AS user_watched_at, r.rating AS my_rating
            FROM user_movies um
            JOIN movies m ON m.id=um.movie_id
            LEFT JOIN ratings r ON r.movie_id=m.id AND r.user_id=um.user_id
            WHERE um.user_id=? AND um.movie_id=?
        """, (user_id, movie_id))
        row = await cursor.fetchone()
        return _movie_dict(row) if row else None


async def get_movies_by_status(
    user_id: int, status: str, limit: int = 20, offset: int = 0, sort: str = "date",
) -> list[dict]:
    """Личный список пользователя. «Топ» — просмотренные фильмы с его оценкой."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        order = "um.added_at DESC"
        where = "um.status='want_to_watch'"
        if status == "watched":
            where = "um.status='watched'"
            order = "r.rating DESC, um.watched_at DESC" if sort == "rating" else "um.watched_at DESC"
        elif status == "top":
            where = "um.status='watched' AND r.rating IS NOT NULL"
            order = "r.rating DESC, um.watched_at DESC"
        cursor = await db.execute(f"""
            SELECT m.*, um.status AS user_status, um.added_at AS user_added_at,
                   um.watched_at AS user_watched_at, r.rating AS my_rating
            FROM user_movies um
            JOIN movies m ON m.id=um.movie_id
            LEFT JOIN ratings r ON r.movie_id=m.id AND r.user_id=um.user_id
            WHERE um.user_id=? AND {where}
            ORDER BY {order} LIMIT ? OFFSET ?
        """, (user_id, limit, offset))
        return [_movie_dict(row) for row in await cursor.fetchall()]


async def count_movies(user_id: int, status: str) -> int:
    async with _connect() as db:
        if status == "top":
            cursor = await db.execute("""
                SELECT COUNT(*) FROM user_movies um
                JOIN ratings r ON r.movie_id=um.movie_id AND r.user_id=um.user_id
                WHERE um.user_id=? AND um.status='watched'
            """, (user_id,))
        else:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM user_movies WHERE user_id=? AND status=?",
                (user_id, status),
            )
        return (await cursor.fetchone())[0]


async def mark_watched(user_id: int, movie_id: int) -> None:
    async with _connect() as db:
        await db.execute("""
            UPDATE user_movies SET status='watched', watched_at=?
            WHERE user_id=? AND movie_id=?
        """, (_now(), user_id, movie_id))
        await db.commit()


async def unmark_watched(user_id: int, movie_id: int) -> None:
    async with _connect() as db:
        await db.execute("""
            UPDATE user_movies SET status='want_to_watch', watched_at=NULL
            WHERE user_id=? AND movie_id=?
        """, (user_id, movie_id))
        await db.commit()


async def delete_user_movie(user_id: int, movie_id: int) -> None:
    """Удаляет фильм только из личного списка, каталог и данные партнёра остаются."""
    async with _connect() as db:
        await db.execute("DELETE FROM ratings WHERE movie_id=? AND user_id=?", (movie_id, user_id))
        await db.execute("DELETE FROM comments WHERE movie_id=? AND user_id=?", (movie_id, user_id))
        await db.execute("DELETE FROM user_movies WHERE user_id=? AND movie_id=?", (user_id, movie_id))
        await db.commit()


async def set_rating(movie_id: int, user_id: int, rating: int) -> None:
    async with _connect() as db:
        await db.execute("""
            INSERT INTO ratings (movie_id, user_id, rating, rated_at) VALUES (?,?,?,?)
            ON CONFLICT(movie_id, user_id) DO UPDATE SET rating=excluded.rating, rated_at=excluded.rated_at
        """, (movie_id, user_id, rating, _now()))
        await db.commit()


async def import_rating(
    movie_id: int, user_id: int, rating: int, rated_at: str | None,
) -> bool:
    """Копирует историческую оценку только если пользователь ещё не поставил новую."""
    async with _connect() as db:
        cursor = await db.execute("""
            INSERT INTO ratings (movie_id, user_id, rating, rated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(movie_id, user_id) DO NOTHING
        """, (movie_id, user_id, rating, rated_at or _now()))
        await db.commit()
        return cursor.rowcount == 1


async def get_ratings(movie_id: int, user_id: int) -> dict[int, int]:
    """Личная оценка: оценки партнёра не раскрываются вне общей статистики."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, rating FROM ratings WHERE movie_id=? AND user_id=?",
            (movie_id, user_id),
        )
        return {row["user_id"]: row["rating"] for row in await cursor.fetchall()}


async def set_comment(movie_id: int, user_id: int, text: str) -> None:
    async with _connect() as db:
        await db.execute("""
            INSERT INTO comments (movie_id, user_id, text, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(movie_id, user_id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at
        """, (movie_id, user_id, text, _now()))
        await db.commit()


async def import_comment(
    movie_id: int, user_id: int, text: str, updated_at: str | None,
) -> bool:
    """Копирует комментарий из старого бота, не затирая более новый комментарий в Mini App."""
    async with _connect() as db:
        cursor = await db.execute("""
            INSERT INTO comments (movie_id, user_id, text, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(movie_id, user_id) DO NOTHING
        """, (movie_id, user_id, text, updated_at or _now()))
        await db.commit()
        return cursor.rowcount == 1


async def import_partner_invite(invite: dict) -> bool:
    """Сохраняет историческое приглашение без изменения уже существующей записи."""
    async with _connect() as db:
        cursor = await db.execute("""
            INSERT INTO partner_invites (
                token, inviter_id, status, created_at, expires_at, accepted_by, accepted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token) DO NOTHING
        """, (
            invite["token"],
            invite["inviter_id"],
            invite["status"],
            invite["created_at"],
            invite["expires_at"],
            invite.get("accepted_by"),
            invite.get("accepted_at"),
        ))
        await db.commit()
        return cursor.rowcount == 1


async def delete_comment(movie_id: int, user_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM comments WHERE movie_id=? AND user_id=?", (movie_id, user_id))
        await db.commit()


async def get_comments(movie_id: int, user_id: int) -> dict[int, str]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, text FROM comments WHERE movie_id=? AND user_id=?",
            (movie_id, user_id),
        )
        return {row["user_id"]: row["text"] for row in await cursor.fetchall()}


async def get_random_want(user_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT m.*, um.status AS user_status, um.added_at AS user_added_at,
                   um.watched_at AS user_watched_at, r.rating AS my_rating
            FROM user_movies um
            JOIN movies m ON m.id=um.movie_id
            LEFT JOIN ratings r ON r.movie_id=m.id AND r.user_id=um.user_id
            WHERE um.user_id=? AND um.status='want_to_watch'
            ORDER BY RANDOM() LIMIT 1
        """, (user_id,))
        row = await cursor.fetchone()
        return _movie_dict(row) if row else None


def _metadata_stats(rows: list[aiosqlite.Row]) -> dict:
    genre_counts: dict[str, int] = {}
    actor_counts: dict[str, int] = {}
    director_counts: dict[str, int] = {}
    total_runtime_min = 0
    for row in rows:
        for genre in (row["genres"] or "").split(","):
            genre = genre.strip()
            if genre and genre != "N/A":
                genre_counts[genre] = genre_counts.get(genre, 0) + 1
        for actor in (row["actors"] or "").split(","):
            actor = actor.strip()
            if actor:
                actor_counts[actor] = actor_counts.get(actor, 0) + 1
        for director in (row["directors"] or "").split(","):
            director = director.strip()
            if director:
                director_counts[director] = director_counts.get(director, 0) + 1
        runtime = re.search(r"\d+", row["runtime"] or "")
        if runtime:
            total_runtime_min += int(runtime.group(0))

    total_genres = sum(genre_counts.values())
    return {
        "total_runtime_min": total_runtime_min,
        "top_genres_pct": [
            (genre, round(count / total_genres * 100))
            for genre, count in sorted(genre_counts.items(), key=lambda item: -item[1])[:5]
        ] if total_genres else [],
        "top_actors": [
            (name, count) for name, count in sorted(actor_counts.items(), key=lambda item: -item[1])[:5]
        ],
        "top_directors": [
            (name, count) for name, count in sorted(director_counts.items(), key=lambda item: -item[1])[:3]
        ],
    }


async def get_personal_stats(user_id: int) -> dict:
    """Статистика пользователя только по его личным фильмам и оценкам."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        watched = (await (await db.execute(
            "SELECT COUNT(*) count FROM user_movies WHERE user_id=? AND status='watched'", (user_id,)
        )).fetchone())["count"]
        want = (await (await db.execute(
            "SELECT COUNT(*) count FROM user_movies WHERE user_id=? AND status='want_to_watch'", (user_id,)
        )).fetchone())["count"]
        rating_row = await (await db.execute("""
            SELECT AVG(r.rating) average, COUNT(r.id) count FROM ratings r
            JOIN user_movies um ON um.movie_id=r.movie_id AND um.user_id=r.user_id
            WHERE r.user_id=? AND um.status='watched'
        """, (user_id,))).fetchone()
        rows = await (await db.execute("""
            SELECT m.genres, m.actors, m.directors, m.runtime FROM user_movies um
            JOIN movies m ON m.id=um.movie_id
            WHERE um.user_id=? AND um.status='watched'
        """, (user_id,))).fetchall()
        latest = await (await db.execute("""
            SELECT m.title FROM user_movies um JOIN movies m ON m.id=um.movie_id
            WHERE um.user_id=? AND um.status='watched'
            ORDER BY um.watched_at DESC LIMIT 1
        """, (user_id,))).fetchone()
        result = _metadata_stats(rows)
        result.update({
            "watched": watched,
            "want": want,
            "ratings_count": rating_row["count"],
            "avg_rating": round(rating_row["average"], 1) if rating_row["average"] is not None else None,
            "last_watched": latest["title"] if latest else None,
        })
        return result


async def get_year_stats(user_id: int, year: int) -> dict:
    """Личные итоги года с честным отображением всех фильмов с лучшей оценкой."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        pattern = f"{year}-%"
        rows = await (await db.execute("""
            SELECT m.genres, m.actors, m.directors, m.runtime FROM user_movies um
            JOIN movies m ON m.id=um.movie_id
            WHERE um.user_id=? AND um.status='watched' AND um.watched_at LIKE ?
        """, (user_id, pattern))).fetchall()
        metadata = _metadata_stats(rows)
        rated = await (await db.execute("""
            SELECT m.title, r.rating FROM user_movies um
            JOIN movies m ON m.id=um.movie_id
            JOIN ratings r ON r.movie_id=um.movie_id AND r.user_id=um.user_id
            WHERE um.user_id=? AND um.status='watched' AND um.watched_at LIKE ?
            ORDER BY r.rating DESC
        """, (user_id, pattern))).fetchall()
        best_rating = rated[0]["rating"] if rated else None
        return {
            "year": year,
            "count": len(rows),
            "total_runtime_min": metadata["total_runtime_min"],
            "top_genres_pct": metadata["top_genres_pct"],
            "best_rating": best_rating,
            "best_titles": [row["title"] for row in rated if row["rating"] == best_rating],
        }


async def get_pair_stats(user1_id: int, user2_id: int) -> dict:
    """Общая статистика двух партнёров без раскрытия их личных списков."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        shared_rows = await (await db.execute("""
            SELECT m.id, m.title, m.genres, m.actors, m.directors, m.runtime
            FROM movies m
            JOIN user_movies first ON first.movie_id=m.id AND first.user_id=? AND first.status='watched'
            JOIN user_movies second ON second.movie_id=m.id AND second.user_id=? AND second.status='watched'
        """, (user1_id, user2_id))).fetchall()
        rated_rows = await (await db.execute("""
            SELECT m.title, r1.rating AS first_rating, r2.rating AS second_rating,
                   ABS(r1.rating-r2.rating) AS diff
            FROM movies m
            JOIN user_movies first ON first.movie_id=m.id AND first.user_id=? AND first.status='watched'
            JOIN user_movies second ON second.movie_id=m.id AND second.user_id=? AND second.status='watched'
            JOIN ratings r1 ON r1.movie_id=m.id AND r1.user_id=?
            JOIN ratings r2 ON r2.movie_id=m.id AND r2.user_id=?
            ORDER BY diff DESC, m.title
        """, (user1_id, user2_id, user1_id, user2_id))).fetchall()
        metadata = _metadata_stats(shared_rows)
        count = len(rated_rows)
        average_diff = sum(row["diff"] for row in rated_rows) / count if count else None
        equal = [row for row in rated_rows if row["diff"] == 0]
        best_average = max(
            ((row["first_rating"] + row["second_rating"]) / 2 for row in rated_rows),
            default=None,
        )
        return {
            "shared_watched": len(shared_rows),
            "shared_runtime_min": metadata["total_runtime_min"],
            "top_genres_pct": metadata["top_genres_pct"],
            "top_actors": metadata["top_actors"],
            "top_directors": metadata["top_directors"],
            "compatibility": {
                "count": count,
                "agreement": round(100 - average_diff / 9 * 100) if average_diff is not None else None,
            },
            "perfect_match": dict(equal[0]) if equal else None,
            "taste_match_count": len(equal),
            "controversial": dict(rated_rows[0]) if rated_rows else None,
            "best_together": [
                row["title"] for row in rated_rows
                if best_average is not None and (row["first_rating"] + row["second_rating"]) / 2 == best_average
            ],
        }


async def get_unrated_watched(user_id: int, since_days: int = 30, limit: int = 10) -> list[dict]:
    """Личные просмотренные фильмы без оценки — для будущих напоминаний."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT m.id, m.title FROM user_movies um
            JOIN movies m ON m.id=um.movie_id
            LEFT JOIN ratings r ON r.movie_id=um.movie_id AND r.user_id=um.user_id
            WHERE um.user_id=? AND um.status='watched' AND um.watched_at >= ? AND r.id IS NULL
            ORDER BY um.watched_at DESC LIMIT ?
        """, (user_id, cutoff, limit))
        return [dict(row) for row in await cursor.fetchall()]


async def healthcheck() -> None:
    """Проверяет доступность текущей БД для endpoint-а хостинга."""
    async with _connect() as db:
        cursor = await db.execute("SELECT 1")
        if await cursor.fetchone() is None:
            raise RuntimeError("База данных не ответила на проверочный запрос")


async def close_db() -> None:
    """Освобождает пул PostgreSQL; SQLite-соединения закрываются на каждом запросе."""
    await db_runtime.close()
