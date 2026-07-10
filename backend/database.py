"""SQLite-слой публичного Mini App (мультитенантность, single-user модель).

Схема разделена на три сущности:
  users       — любой пользователь Telegram (регистрируется при первом входе).
  films        — ОБЩИЙ каталог-кэш (фильм хранится один раз, dedup по imdb_id).
  user_films   — состояние фильма У КОНКРЕТНОГО юзера (статус, оценка, коммент).

Community-рейтинг = средняя оценка всех юзеров по фильму (агрегат user_films).
Проверенные решения из movie_bot сохранены: WAL, VACUUM INTO-бэкапы, честные ничьи.
"""
import glob
import os
import re
import aiosqlite
from datetime import datetime, timezone, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "movies.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Инициализация ────────────────────────────────────────────────────────────
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL: чтение не блокирует запись — публичный трафик без «database is locked».
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY,          -- telegram user id
                first_name TEXT,
                username   TEXT,
                created_at TEXT,
                last_seen  TEXT
            )
        """)
        # Общий каталог фильмов (кэш источников). Заполняется при добавлении/поиске.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS films (
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
                created_at     TEXT
            )
        """)
        # Состояние фильма у пользователя. Одна оценка на пару (user, film).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_films (
                user_id    INTEGER NOT NULL,
                film_id    INTEGER NOT NULL,
                status     TEXT NOT NULL DEFAULT 'want_to_watch',  -- want_to_watch | watched
                rating     INTEGER,                                 -- 1..10, NULL пока не оценил
                comment    TEXT,
                added_at   TEXT,
                watched_at TEXT,
                rated_at   TEXT,
                PRIMARY KEY (user_id, film_id)
            )
        """)
        # Индексы под горячие запросы: список юзера и community-агрегат по фильму.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_user ON user_films(user_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_film ON user_films(film_id)")
        await db.commit()


async def backup_db(keep: int = 7) -> str | None:
    """Консистентный бэкап (VACUUM INTO) рядом с базой; храним последние `keep`."""
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


# ── Пользователи ─────────────────────────────────────────────────────────────
async def upsert_user(user: dict) -> None:
    """Регистрация/обновление любого пользователя Telegram (белого списка нет)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (id, first_name, username, created_at, last_seen)
            VALUES (?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                first_name = excluded.first_name,
                username   = excluded.username,
                last_seen  = excluded.last_seen
            """,
            (user.get("id"), user.get("first_name"), user.get("username"), _now(), _now()),
        )
        await db.commit()


# ── Каталог фильмов ──────────────────────────────────────────────────────────
async def get_or_create_film(
    imdb_id: str, title: str, year: str | None = None, genres: str | None = None,
    runtime: str | None = None, imdb_rating: str | None = None, imdb_votes: str | None = None,
    plot: str | None = None, poster_url: str | None = None, title_original: str | None = None,
    kp_rating: str | None = None, directors: str | None = None, actors: str | None = None,
) -> int:
    """Возвращает id фильма в общем каталоге, создавая запись при первом появлении
    (dedup по imdb_id). Идемпотентно — один фильм на всех пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM films WHERE imdb_id = ?", (imdb_id,))
        row = await cur.fetchone()
        if row:
            return row["id"]
        cur = await db.execute(
            """
            INSERT INTO films
                (imdb_id, title, title_original, year, genres, directors, actors, runtime,
                 imdb_rating, kp_rating, imdb_votes, plot, poster_url, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(imdb_id) DO NOTHING
            """,
            (imdb_id, title, title_original, year, genres, directors, actors, runtime,
             imdb_rating, kp_rating, imdb_votes, plot, poster_url, _now()),
        )
        await db.commit()
        if cur.lastrowid:
            return cur.lastrowid
        # Гонка: кто-то вставил параллельно — перечитываем.
        cur = await db.execute("SELECT id FROM films WHERE imdb_id = ?", (imdb_id,))
        return (await cur.fetchone())["id"]


async def get_film(film_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM films WHERE id = ?", (film_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_film_by_imdb(imdb_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM films WHERE imdb_id = ?", (imdb_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def community_rating(film_id: int) -> dict:
    """Средняя оценка всех пользователей по фильму + количество оценок."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT AVG(rating) AS avg, COUNT(rating) AS cnt FROM user_films "
            "WHERE film_id = ? AND rating IS NOT NULL", (film_id,))
        row = await cur.fetchone()
        avg, cnt = row[0], row[1] or 0
        return {"avg": round(avg, 1) if avg is not None else None, "count": cnt}


# ── Список пользователя (user_films) ─────────────────────────────────────────
async def add_to_list(user_id: int, film_id: int, status: str = "want_to_watch",
                      watched_at: str | None = None) -> bool:
    """Добавить фильм в свой список. False = уже был у этого пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO user_films (user_id, film_id, status, added_at, watched_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id, film_id) DO NOTHING
            """,
            (user_id, film_id, status, _now(), watched_at),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_user_film(user_id: int, film_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_films WHERE user_id = ? AND film_id = ?", (user_id, film_id))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_rating(user_id: int, film_id: int, rating: int) -> None:
    """Тап по оценке = «просмотрено» (урок). Оценка автоматически добавляет фильм
    в список пользователя, если его там ещё не было."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_films (user_id, film_id, status, rating, added_at, watched_at, rated_at)
            VALUES (?,?, 'watched', ?, ?, ?, ?)
            ON CONFLICT(user_id, film_id) DO UPDATE SET
                rating     = excluded.rating,
                rated_at   = excluded.rated_at,
                status     = 'watched',
                watched_at = COALESCE(user_films.watched_at, excluded.watched_at)
            """,
            (user_id, film_id, rating, _now(), _now(), _now()),
        )
        await db.commit()


async def set_status(user_id: int, film_id: int, status: str) -> None:
    """Сменить статус. Фильм появляется в списке, если его не было. Оценка сохраняется."""
    watched_at = _now() if status == "watched" else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_films (user_id, film_id, status, added_at, watched_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id, film_id) DO UPDATE SET
                status     = excluded.status,
                watched_at = CASE WHEN excluded.status='watched'
                                  THEN COALESCE(user_films.watched_at, excluded.watched_at)
                                  ELSE NULL END
            """,
            (user_id, film_id, status, _now(), watched_at),
        )
        await db.commit()


async def set_comment(user_id: int, film_id: int, text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_films (user_id, film_id, status, comment, added_at)
            VALUES (?,?, 'want_to_watch', ?, ?)
            ON CONFLICT(user_id, film_id) DO UPDATE SET comment = excluded.comment
            """,
            (user_id, film_id, text, _now()),
        )
        await db.commit()


async def delete_comment(user_id: int, film_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_films SET comment = NULL WHERE user_id = ? AND film_id = ?",
            (user_id, film_id))
        await db.commit()


async def remove_from_list(user_id: int, film_id: int) -> None:
    """Убрать фильм из СВОЕГО списка. В общем каталоге films он остаётся."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM user_films WHERE user_id = ? AND film_id = ?", (user_id, film_id))
        await db.commit()


async def get_user_films(user_id: int, status: str, limit: int = 50, offset: int = 0,
                         sort: str = "date") -> list[dict]:
    """Список фильмов пользователя. status: want_to_watch | watched | top.
    top = просмотренные и оценённые этим юзером, по убыванию его оценки."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        base = """
            SELECT f.*, uf.status AS status, uf.rating AS my_rating,
                   uf.comment AS my_comment, uf.added_at AS added_at, uf.watched_at AS watched_at
            FROM user_films uf JOIN films f ON f.id = uf.film_id
            WHERE uf.user_id = ?
        """
        if status == "top":
            cur = await db.execute(
                base + " AND uf.status='watched' AND uf.rating IS NOT NULL "
                       "ORDER BY uf.rating DESC, uf.watched_at DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset))
        elif status == "watched":
            order = "uf.rating DESC" if sort == "rating" else "uf.watched_at DESC"
            cur = await db.execute(
                base + f" AND uf.status='watched' ORDER BY {order} LIMIT ? OFFSET ?",
                (user_id, limit, offset))
        else:
            cur = await db.execute(
                base + " AND uf.status='want_to_watch' ORDER BY uf.added_at DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset))
        return [dict(r) for r in await cur.fetchall()]


async def count_user_films(user_id: int, status: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        if status == "top":
            cur = await db.execute(
                "SELECT COUNT(*) FROM user_films WHERE user_id=? AND status='watched' "
                "AND rating IS NOT NULL", (user_id,))
        else:
            cur = await db.execute(
                "SELECT COUNT(*) FROM user_films WHERE user_id=? AND status=?", (user_id, status))
        return (await cur.fetchone())[0]


async def get_random_want(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT f.*, uf.rating AS my_rating FROM user_films uf JOIN films f ON f.id = uf.film_id
            WHERE uf.user_id = ? AND uf.status = 'want_to_watch' ORDER BY RANDOM() LIMIT 1
            """, (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_unrated_watched(user_id: int, since_days: int = 30, limit: int = 10) -> list[dict]:
    """Просмотренные за N дней, не оценённые пользователем (для напоминаний ботом)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT f.id, f.title FROM user_films uf JOIN films f ON f.id = uf.film_id
            WHERE uf.user_id = ? AND uf.status = 'watched' AND uf.rating IS NULL
              AND (uf.watched_at IS NULL OR uf.watched_at >= ?)
            ORDER BY uf.watched_at DESC LIMIT ?
            """, (user_id, cutoff, limit))
        return [dict(r) for r in await cur.fetchall()]


# ── Персональная статистика (без пар) ────────────────────────────────────────
async def get_user_stats(user_id: int) -> dict:
    """Личная статистика пользователя: счётчики, экранное время, средняя оценка,
    топ жанров/актёров/режиссёров (ничьи честно), итоги года."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        watched = (await (await db.execute(
            "SELECT COUNT(*) c FROM user_films WHERE user_id=? AND status='watched'",
            (user_id,))).fetchone())["c"]
        want = (await (await db.execute(
            "SELECT COUNT(*) c FROM user_films WHERE user_id=? AND status='want_to_watch'",
            (user_id,))).fetchone())["c"]

        row = await (await db.execute(
            "SELECT AVG(rating) avg, COUNT(*) cnt FROM user_films "
            "WHERE user_id=? AND rating IS NOT NULL", (user_id,))).fetchone()
        avg_rating = round(row["avg"], 1) if row["avg"] is not None else None
        rating_count = row["cnt"]

        cur = await db.execute(
            """
            SELECT f.genres, f.actors, f.directors, f.runtime
            FROM user_films uf JOIN films f ON f.id = uf.film_id
            WHERE uf.user_id = ? AND uf.status = 'watched'
            """, (user_id,))
        genre_counts, actor_counts, director_counts = {}, {}, {}
        total_runtime_min = 0
        for r in await cur.fetchall():
            for g in (r["genres"] or "").split(","):
                g = g.strip()
                if g and g != "N/A":
                    genre_counts[g] = genre_counts.get(g, 0) + 1
            for a in (r["actors"] or "").split(","):
                a = a.strip()
                if a:
                    actor_counts[a] = actor_counts.get(a, 0) + 1
            for d in (r["directors"] or "").split(","):
                d = d.strip()
                if d:
                    director_counts[d] = director_counts.get(d, 0) + 1
            m = re.search(r"\d+", r["runtime"] or "")
            if m:
                total_runtime_min += int(m.group(0))

        total_refs = sum(genre_counts.values())
        top_genres_pct = [
            (g, round(c / total_refs * 100))
            for g, c in sorted(genre_counts.items(), key=lambda x: -x[1])[:5]
        ] if total_refs else []
        top_actors = [(n, c) for n, c in sorted(actor_counts.items(), key=lambda x: -x[1]) if c >= 2][:5]
        top_directors = [(n, c) for n, c in sorted(director_counts.items(), key=lambda x: -x[1]) if c >= 2][:3]

        row = await (await db.execute(
            "SELECT f.title FROM user_films uf JOIN films f ON f.id = uf.film_id "
            "WHERE uf.user_id=? AND uf.status='watched' ORDER BY uf.watched_at DESC LIMIT 1",
            (user_id,))).fetchone()
        last_watched = row["title"] if row else None

        return {
            "watched": watched,
            "want": want,
            "avg_rating": avg_rating,
            "rating_count": rating_count,
            "total_runtime_min": total_runtime_min,
            "top_genres_pct": top_genres_pct,
            "top_actors": top_actors,
            "top_directors": top_directors,
            "last_watched": last_watched,
        }


async def get_year_stats(user_id: int, year: int) -> dict:
    """Личные итоги года. Ничьи — честно: список лучших, «актёр года» только при
    единоличном лидерстве (урок: случайный «первый из пяти» — это ложь)."""
    like = f"{year}-%"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT f.runtime, f.genres, f.actors, uf.rating, f.title
            FROM user_films uf JOIN films f ON f.id = uf.film_id
            WHERE uf.user_id = ? AND uf.status='watched' AND uf.watched_at LIKE ?
            """, (user_id, like))
        rows = await cur.fetchall()

        total_min, genre_counts, actor_counts = 0, {}, {}
        ratings = []
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
            if r["rating"] is not None:
                ratings.append((r["title"], r["rating"]))

        top_genre = max(genre_counts.items(), key=lambda x: x[1])[0] if genre_counts else None
        ranked = sorted(actor_counts.items(), key=lambda x: -x[1])
        top_actor = None
        if ranked and ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            top_actor = ranked[0]

        avg_rating = round(sum(r for _, r in ratings) / len(ratings), 1) if ratings else None
        best = max((r for _, r in ratings), default=None)
        best_titles = [t for t, r in ratings if r == best] if best is not None else []

        return {
            "year": year,
            "count": len(rows),
            "total_runtime_min": total_min,
            "top_genre": top_genre,
            "top_actor": top_actor,
            "avg_rating": avg_rating,
            "best_avg": best,
            "best_titles": best_titles,
        }


# ── Discovery: публичный каталог (Фаза C) ────────────────────────────────────
# Минимум оценок, чтобы фильм попал в «Топ спильноты» (честность: топ из одной
# случайной оценки — ложь). Пока база пользователей мала — 1; поднять при росте.
MIN_COMMUNITY_VOTES = int(os.getenv("MIN_COMMUNITY_VOTES", "1"))


def _browse_dict(row) -> dict:
    """Строка каталога -> нормализованный item с community-рейтингом и моим статусом."""
    d = dict(row)
    avg = d.pop("community_avg", None)
    d["community"] = {"avg": round(avg, 1) if avg is not None else None,
                      "count": d.pop("community_count", 0) or 0}
    d["popularity"] = d.get("popularity", 0) or 0
    d["in_list"] = d.pop("my_status", None) is not None
    return d


# Корреляционные подзапросы: community и популярность фильма; LEFT JOIN — мой статус.
_BROWSE_COLS = """
    f.*,
    (SELECT AVG(rating) FROM user_films WHERE film_id=f.id AND rating IS NOT NULL) AS community_avg,
    (SELECT COUNT(rating) FROM user_films WHERE film_id=f.id AND rating IS NOT NULL) AS community_count,
    (SELECT COUNT(*) FROM user_films WHERE film_id=f.id) AS popularity,
    me.status AS my_status, me.rating AS my_rating
"""


async def browse_popular(user_id: int, limit: int = 30, offset: int = 0) -> list[dict]:
    """Популярное: по числу пользователей, добавивших фильм."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT {_BROWSE_COLS}
            FROM films f
            LEFT JOIN user_films me ON me.film_id = f.id AND me.user_id = ?
            ORDER BY popularity DESC, f.created_at DESC
            LIMIT ? OFFSET ?
            """, (user_id, limit, offset))
        return [_browse_dict(r) for r in await cur.fetchall()]


async def browse_top(user_id: int, limit: int = 30, offset: int = 0,
                     min_votes: int | None = None) -> list[dict]:
    """Топ спильноты: по средней оценке всех пользователей (min_votes — честный порог)."""
    mv = MIN_COMMUNITY_VOTES if min_votes is None else min_votes
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT f.*,
                   AVG(uf.rating) AS community_avg,
                   COUNT(uf.rating) AS community_count,
                   (SELECT COUNT(*) FROM user_films WHERE film_id=f.id) AS popularity,
                   me.status AS my_status, me.rating AS my_rating
            FROM films f
            JOIN user_films uf ON uf.film_id = f.id AND uf.rating IS NOT NULL
            LEFT JOIN user_films me ON me.film_id = f.id AND me.user_id = ?
            GROUP BY f.id
            HAVING COUNT(uf.rating) >= ?
            ORDER BY community_avg DESC, community_count DESC
            LIMIT ? OFFSET ?
            """, (user_id, mv, limit, offset))
        return [_browse_dict(r) for r in await cur.fetchall()]


async def browse_by_genre(user_id: int, genre: str, limit: int = 30, offset: int = 0) -> list[dict]:
    """Каталог по жанру (подстрока в поле genres), по популярности."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT {_BROWSE_COLS}
            FROM films f
            LEFT JOIN user_films me ON me.film_id = f.id AND me.user_id = ?
            WHERE f.genres LIKE '%' || ? || '%'
            ORDER BY popularity DESC, f.created_at DESC
            LIMIT ? OFFSET ?
            """, (user_id, genre, limit, offset))
        return [_browse_dict(r) for r in await cur.fetchall()]


async def list_genres() -> list[dict]:
    """Жанры, присутствующие в каталоге, по убыванию частоты: [{name, count}]."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT genres FROM films")
        counts: dict[str, int] = {}
        for r in await cur.fetchall():
            for g in (r["genres"] or "").split(","):
                g = g.strip()
                if g and g != "N/A":
                    counts[g] = counts.get(g, 0) + 1
        return [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda x: -x[1])]
