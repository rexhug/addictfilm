"""SQLite-слой публичного Mini App (мультитенантность, single-user модель).

Схема разделена на три сущности:
  users       — любой пользователь Telegram (регистрируется при первом входе).
  films        — ОБЩИЙ каталог-кэш (фильм хранится один раз, dedup по imdb_id).
  user_films   — состояние фильма У КОНКРЕТНОГО юзера (статус, оценка, коммент).

Community-рейтинг = средняя оценка всех юзеров по фильму (агрегат user_films).
Проверенные решения из movie_bot сохранены: WAL, VACUUM INTO-бэкапы, честные ничьи.
"""
import glob
import json
import os
import re
import secrets
import aiosqlite
import db_runtime
from datetime import datetime, timezone, timedelta
from config import DATABASE_URL

# SQLite локально (DB_PATH=movies.db рядом с проектом); в облаке — Postgres (Neon)
# через DATABASE_URL, см. db_runtime.py. DB_PATH используется только для SQLite-режима.
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "movies.db"))
_PG = db_runtime.uses_postgres(DATABASE_URL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _add_column_if_missing(table: str, col_def: str) -> None:
    """ALTER TABLE ADD COLUMN, идемпотентно. Каждая попытка — В СВОЁМ соединении/
    транзакции: под Postgres любая ошибка (например «колонка уже есть») переводит
    ВСЮ транзакцию в aborted-состояние — если бы это было внутри общего блока
    init_db(), она утянула бы за собой все последующие CREATE TABLE IF NOT EXISTS."""
    try:
        async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            await db.commit()
    except Exception:
        pass


# ── Инициализация ────────────────────────────────────────────────────────────
async def init_db() -> None:
    # Миграция для БД, созданных до backdrop_url/age_rating/actors_photos/role — до
    # основного блока и в изолированных транзакциях (см. _add_column_if_missing).
    await _add_column_if_missing("films", "backdrop_url TEXT")
    await _add_column_if_missing("films", "age_rating TEXT")
    await _add_column_if_missing("films", "actors_photos TEXT")
    await _add_column_if_missing("users", "role TEXT")
    await _add_column_if_missing("users", "photo_url TEXT")

    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        # WAL: чтение не блокирует запись — публичный трафик без «database is locked».
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         BIGINT PRIMARY KEY,          -- telegram user id (>int32, нужен BIGINT)
                first_name TEXT,
                username   TEXT,
                role       TEXT,
                photo_url  TEXT,
                created_at TEXT,
                last_seen  TEXT
            )
        """)
        # Общий каталог фильмов (кэш источников). Заполняется при добавлении/поиске.
        # AUTOINCREMENT — SQLite-синтаксис; в Postgres автоинкремент даёт SERIAL.
        _film_id_col = "SERIAL PRIMARY KEY" if _PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS films (
                id             {_film_id_col},
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
                backdrop_url   TEXT,
                age_rating     TEXT,
                actors_photos  TEXT,   -- JSON-массив name/photo_url под тех же актёров, что в actors
                created_at     TEXT
            )
        """)
        # Состояние фильма у пользователя. Одна оценка на пару (user, film).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_films (
                user_id    BIGINT NOT NULL,               -- telegram user id (>int32)
                film_id    INTEGER NOT NULL,
                status     TEXT NOT NULL DEFAULT 'want_to_watch'
                           CHECK (status IN ('want_to_watch', 'watched')),
                rating     INTEGER CHECK (rating BETWEEN 1 AND 10),
                comment    TEXT,
                added_at   TEXT,
                watched_at TEXT,
                rated_at   TEXT,
                PRIMARY KEY (user_id, film_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (film_id) REFERENCES films(id)
            )
        """)
        # Постоянный кэш поисковых запросов: повторный поиск не бьёт в API источника
        # (переживает рестарты/деплои, общий на всех). Экономит суточный лимит kinopoisk.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS search_cache (
                q          TEXT PRIMARY KEY,   -- нормализованный запрос
                results    TEXT NOT NULL,      -- JSON: список нормализованных item'ов
                created_at TEXT NOT NULL
            )
        """)
        # Дневной бюджет внешних поисковых вызовов — общий на все инстансы (иначе при
        # 2+ Fly-машинах каждый процесс тратил бы свой отдельный бюджет). 1 строка/день.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS search_budget (
                day   TEXT PRIMARY KEY,
                spent INTEGER NOT NULL DEFAULT 0
            )
        """)
        # ── Пара (Фаза E): приглашения + активные пары ──────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS partner_invites (
                token      TEXT PRIMARY KEY,
                from_user  BIGINT NOT NULL,               -- telegram user id (>int32)
                status     TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted
                created_at TEXT NOT NULL,
                FOREIGN KEY (from_user) REFERENCES users(id)
            )
        """)
        # Симметрично: для пары (a,b) две строки — a→b и b→a (лукап по user_id O(1)).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS partners (
                user_id    BIGINT PRIMARY KEY,           -- telegram user id (>int32)
                partner_id BIGINT NOT NULL,
                since      TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (partner_id) REFERENCES users(id),
                CHECK (user_id <> partner_id)
            )
        """)
        # ── Подборки (кураторские коллекции фильмов от админа/редактора) ────────
        _coll_id_col = "SERIAL PRIMARY KEY" if _PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS collections (
                id         {_coll_id_col},
                title      TEXT NOT NULL,
                created_by BIGINT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (created_by) REFERENCES users(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS collection_films (
                collection_id INTEGER NOT NULL,
                film_id       INTEGER NOT NULL,
                added_at      TEXT NOT NULL,
                PRIMARY KEY (collection_id, film_id),
                FOREIGN KEY (collection_id) REFERENCES collections(id),
                FOREIGN KEY (film_id) REFERENCES films(id)
            )
        """)

        # Индексы под горячие запросы: список юзера и community-агрегат по фильму.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_user ON user_films(user_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_film ON user_films(film_id)")
        # Покрывают реальные ORDER BY списков, не меняя схему или данные.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_user_added ON user_films(user_id, status, added_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_user_watched ON user_films(user_id, status, watched_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_user_top ON user_films(user_id, status, rating DESC, watched_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_invite_from ON partner_invites(from_user, status)")
        # Ранние версии могли создать несколько pending-инвайтов из-за гонки
        # check-then-insert. Историю сохраняем, но оставляем рабочим только самый
        # новый токен каждого пользователя, после чего индекс делает инвариант
        # атомарным на всех инстансах.
        await db.execute("""
            UPDATE partner_invites AS stale
            SET status = 'superseded'
            WHERE stale.status = 'pending'
              AND EXISTS (
                  SELECT 1
                  FROM partner_invites AS newer
                  WHERE newer.from_user = stale.from_user
                    AND newer.status = 'pending'
                    AND (newer.created_at > stale.created_at
                         OR (newer.created_at = stale.created_at AND newer.token > stale.token))
              )
        """)
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_invite_one_pending_per_user "
            "ON partner_invites(from_user) WHERE status = 'pending'"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_cf_collection ON collection_films(collection_id)")
        await db.commit()


async def ping() -> bool:
    """Лёгкая readiness-проверка доступности активной базы данных."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute("SELECT 1")
        return await cur.fetchone() is not None


# ── Постоянный кэш поиска ─────────────────────────────────────────────────────
async def search_cache_get(q: str, max_age_sec: int) -> list | None:
    """Свежие (моложе max_age_sec) результаты поиска по нормализованному запросу, либо None."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT results, created_at FROM search_cache WHERE q = ?", (q,))
        row = await cur.fetchone()
    if not row:
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])).total_seconds()
    except ValueError:
        return None
    if age > max_age_sec:
        return None
    try:
        return json.loads(row["results"])
    except json.JSONDecodeError:
        return None


async def search_cache_put(q: str, results: list) -> None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "INSERT INTO search_cache (q, results, created_at) VALUES (?,?,?) "
            "ON CONFLICT(q) DO UPDATE SET results = excluded.results, created_at = excluded.created_at",
            (q, json.dumps(results, ensure_ascii=False), _now()))
        await db.commit()


async def purge_search_cache(max_age_sec: int) -> int:
    """Удалить протухшие записи кэша поиска (иначе таблица растёт без границы).
    Возвращает число удалённых строк."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_sec)).isoformat()
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute("DELETE FROM search_cache WHERE created_at < ?", (cutoff,))
        await db.commit()
        return cur.rowcount


async def try_spend_search_budget(day: str, budget: int) -> bool:
    """Атомарный инкремент дневного бюджета внешних вызовов kinopoisk/OMDb — общий
    на все инстансы (важно при 2+ Fly-машинах). True — единица списана, False — бюджет
    на сегодня исчерпан. UPSERT с условием в WHERE — атомарно и без гонок в обоих движках."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "INSERT INTO search_budget (day, spent) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET spent = search_budget.spent + 1 "
            "WHERE search_budget.spent < ? "
            "RETURNING spent",
            (day, budget))
        row = await cur.fetchone()
        await db.commit()
        return row is not None


async def backup_db(keep: int = 7) -> str | None:
    """Консистентный бэкап (VACUUM INTO) рядом с базой; храним последние `keep`.
    SQLite-only — в Postgres (Neon) бэкапы делает сам провайдер (point-in-time restore)."""
    if _PG:
        return None
    dirname = os.path.dirname(os.path.abspath(DB_PATH))
    path = os.path.join(dirname, f"movies.backup-{datetime.now(timezone.utc):%Y%m%d}.db")
    if not os.path.exists(path):
        try:
            async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            """
            INSERT INTO users (id, first_name, username, photo_url, created_at, last_seen)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                first_name = excluded.first_name,
                username   = excluded.username,
                photo_url  = COALESCE(excluded.photo_url, users.photo_url),
                last_seen  = excluded.last_seen
            """,
            (user.get("id"), user.get("first_name"), user.get("username"), user.get("photo_url"), _now(), _now()),
        )
        await db.commit()


# ── Каталог фильмов ──────────────────────────────────────────────────────────
async def get_or_create_film(
    imdb_id: str, title: str, year: str | None = None, genres: str | None = None,
    runtime: str | None = None, imdb_rating: str | None = None, imdb_votes: str | None = None,
    plot: str | None = None, poster_url: str | None = None, title_original: str | None = None,
    kp_rating: str | None = None, directors: str | None = None, actors: str | None = None,
    backdrop_url: str | None = None, age_rating: str | None = None, actors_photos: str | None = None,
) -> int:
    """Возвращает id фильма в общем каталоге, создавая запись при первом появлении
    (dedup по imdb_id). Идемпотентно — один фильм на всех пользователей."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM films WHERE imdb_id = ?", (imdb_id,))
        row = await cur.fetchone()
        if row:
            return row["id"]
        # RETURNING id вместо lastrowid — портируемо (SQLite 3.35+ и Postgres одинаково).
        cur = await db.execute(
            """
            INSERT INTO films
                (imdb_id, title, title_original, year, genres, directors, actors, runtime,
                 imdb_rating, kp_rating, imdb_votes, plot, poster_url, backdrop_url, age_rating,
                 actors_photos, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(imdb_id) DO NOTHING
            RETURNING id
            """,
            (imdb_id, title, title_original, year, genres, directors, actors, runtime,
             imdb_rating, kp_rating, imdb_votes, plot, poster_url, backdrop_url, age_rating,
             actors_photos, _now()),
        )
        inserted = await cur.fetchone()
        await db.commit()
        if inserted:
            return inserted["id"]
        # Гонка: кто-то вставил параллельно — перечитываем.
        cur = await db.execute("SELECT id FROM films WHERE imdb_id = ?", (imdb_id,))
        return (await cur.fetchone())["id"]


async def get_film(film_id: int) -> dict | None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM films WHERE id = ?", (film_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_film_artwork(imdb_id: str, backdrop_url: str | None,
                           age_rating: str | None = None) -> bool:
    """Записать найденный backdrop для уже существующего фильма.

    Не затираем ранее сохранённые данные: обогащение вызывается лениво при
    открытии карточки фильма и должно быть безопасным при повторных запросах.
    """
    if not imdb_id or not backdrop_url:
        return False
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE films SET backdrop_url = ?, age_rating = COALESCE(age_rating, ?) "
            "WHERE imdb_id = ? AND (backdrop_url IS NULL OR backdrop_url = '')",
            (backdrop_url, age_rating, imdb_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_film_id_by_imdb(imdb_id: str) -> int | None:
    """id фильма в каталоге по imdb_id, если уже есть (без вставки). Нужно, чтобы
    /api/add не ходил в внешние API за фильмом, который уже в каталоге."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute("SELECT id FROM films WHERE imdb_id = ?", (imdb_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def films_missing_poster(limit: int = 200) -> list[dict]:
    """Фильмы каталога без постера (poster_url NULL/пусто) — для бекфила.
    Возвращает [{id, imdb_id, title, title_original, year}] (последние два нужны
    для добора по названию). Свежие сверху (чаще всего нужны первыми)."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, imdb_id, title, title_original, year FROM films "
            "WHERE poster_url IS NULL OR poster_url = '' "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def set_film_poster(imdb_id: str, poster_url: str) -> bool:
    """Проставить постер фильму по imdb_id. Только если его ещё нет (бекфил не
    затирает уже найденное). True = запись обновлена."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE films SET poster_url = ? "
            "WHERE imdb_id = ? AND (poster_url IS NULL OR poster_url = '')",
            (poster_url, imdb_id))
        await db.commit()
        return cur.rowcount > 0


async def films_with_omdb_poster(limit: int = 200) -> list[dict]:
    """Фильмы с постером Amazon/OMDb (заметно хуже качеством, чем кинопоиск) —
    кандидаты на апгрейд. Возвращает [{id, imdb_id, title, title_original, year}]."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, imdb_id, title, title_original, year FROM films "
            "WHERE poster_url LIKE '%media-amazon.com%' "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def upgrade_film_poster(imdb_id: str, poster_url: str) -> bool:
    """Заменить постер фильма на лучший (используется только когда нашли
    kinopoisk-версию взамен OMDb) — в отличие от set_film_poster, ЗАТИРАЕТ
    существующее значение. True = запись обновлена."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE films SET poster_url = ? WHERE imdb_id = ?", (poster_url, imdb_id))
        await db.commit()
        return cur.rowcount > 0


async def films_missing_actor_photos(limit: int = 200) -> list[dict]:
    """Фильмы каталога без фото актёров (для бекфила). [{id, imdb_id}]."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, imdb_id FROM films WHERE imdb_id LIKE 'tt%' "
            "AND (actors_photos IS NULL OR actors_photos = '') "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def set_actor_photos(imdb_id: str, photos_json: str) -> bool:
    """Проставить JSON фото актёров, только если ещё не было. True = обновлено."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE films SET actors_photos = ? "
            "WHERE imdb_id = ? AND (actors_photos IS NULL OR actors_photos = '')",
            (photos_json, imdb_id))
        await db.commit()
        return cur.rowcount > 0


async def community_rating(film_id: int) -> dict:
    """Средняя оценка всех пользователей по фильму + количество оценок."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_films WHERE user_id = ? AND film_id = ?", (user_id, film_id))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_rating(user_id: int, film_id: int, rating: int) -> None:
    """Тап по оценке = «просмотрено» (урок). Оценка автоматически добавляет фильм
    в список пользователя, если его там ещё не было."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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


async def clear_rating(user_id: int, film_id: int) -> None:
    """Убрать оценку (повторный тап по своей звезде) — статус («Смотрел») не трогаем,
    только сама оценка пропадает. Ничего не создаёт, если записи ещё не было."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "UPDATE user_films SET rating = NULL, rated_at = NULL WHERE user_id = ? AND film_id = ?",
            (user_id, film_id),
        )
        await db.commit()


async def set_status(user_id: int, film_id: int, status: str) -> None:
    """Сменить статус. Фильм появляется в списке, если его не было. Оценка сохраняется."""
    watched_at = _now() if status == "watched" else None
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "UPDATE user_films SET comment = NULL WHERE user_id = ? AND film_id = ?",
            (user_id, film_id))
        await db.commit()


async def remove_from_list(user_id: int, film_id: int) -> None:
    """Убрать фильм из СВОЕГО списка. В общем каталоге films он остаётся."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "DELETE FROM user_films WHERE user_id = ? AND film_id = ?", (user_id, film_id))
        await db.commit()


async def get_user_films(user_id: int, status: str, limit: int = 50, offset: int = 0,
                         sort: str = "date") -> list[dict]:
    """Список фильмов пользователя. status: want_to_watch | watched | top.
    top = просмотренные и оценённые этим юзером, по убыванию его оценки."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        if status == "top":
            cur = await db.execute(
                "SELECT COUNT(*) FROM user_films WHERE user_id=? AND status='watched' "
                "AND rating IS NOT NULL", (user_id,))
        else:
            cur = await db.execute(
                "SELECT COUNT(*) FROM user_films WHERE user_id=? AND status=?", (user_id, status))
        return (await cur.fetchone())[0]


async def get_random_want(user_id: int) -> dict | None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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

        # Распределение моих оценок 1..10 (для гистограммы на экране статистики).
        cur = await db.execute(
            "SELECT rating, COUNT(*) c FROM user_films WHERE user_id=? AND rating IS NOT NULL "
            "GROUP BY rating", (user_id,))
        dist = {r["rating"]: r["c"] for r in await cur.fetchall()}
        rating_dist = [dist.get(i, 0) for i in range(1, 11)]

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
            "rating_dist": rating_dist,
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT f.id AS film_id, f.runtime, f.genres, f.actors, uf.rating, f.title
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
                ratings.append({"film_id": r["film_id"], "title": r["title"], "rating": r["rating"]})

        top_genre = max(genre_counts.items(), key=lambda x: x[1])[0] if genre_counts else None
        ranked = sorted(actor_counts.items(), key=lambda x: -x[1])
        top_actor = None
        if ranked and ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            top_actor = ranked[0]

        avg_rating = round(sum(item["rating"] for item in ratings) / len(ratings), 1) if ratings else None
        best = max((item["rating"] for item in ratings), default=None)
        best_items = [item for item in ratings if item["rating"] == best] if best is not None else []
        best_titles = [item["title"] for item in best_items]

        return {
            "year": year,
            "count": len(rows),
            "total_runtime_min": total_min,
            "top_genre": top_genre,
            "top_actor": top_actor,
            "avg_rating": avg_rating,
            "best_avg": best,
            "best_titles": best_titles,
            "best_films": best_items,
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT f.*,
                   AVG(uf.rating) AS community_avg,
                   COUNT(uf.rating) AS community_count,
                   (SELECT COUNT(*) FROM user_films WHERE film_id=f.id) AS popularity,
                   MAX(me.status) AS my_status, MAX(me.rating) AS my_rating
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
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
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT genres FROM films")
        counts: dict[str, int] = {}
        for r in await cur.fetchall():
            for g in (r["genres"] or "").split(","):
                g = g.strip()
                if g and g != "N/A":
                    counts[g] = counts.get(g, 0) + 1
        return [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda x: -x[1])]


# ── Пара (Фаза E): приглашения, пары, совместная статистика ───────────────────
async def get_user(user_id: int) -> dict | None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, first_name, username, photo_url FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_partner(user_id: int) -> int | None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute("SELECT partner_id FROM partners WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def get_pending_invite(from_user: int) -> str | None:
    """Токен своего активного (неиспользованного) приглашения, если есть."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "SELECT token FROM partner_invites WHERE from_user = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1", (from_user,))
        row = await cur.fetchone()
        return row[0] if row else None


async def create_invite(from_user: int) -> str:
    """Создать (или переиспользовать) единственное активное приглашение.

    Частичный уникальный индекс защищает этот инвариант между процессами и
    инстансами. В отличие от прежнего check-then-insert, одновременные запросы
    возвращают один и тот же токен, а не создают два живых.
    """
    token = secrets.token_urlsafe(12)
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "INSERT INTO partner_invites (token, from_user, status, created_at) VALUES (?,?, 'pending', ?) "
            "ON CONFLICT DO NOTHING RETURNING token",
            (token, from_user, _now()))
        created = await cur.fetchone()
        if created:
            await db.commit()
            return created[0]

        cur = await db.execute(
            "SELECT token FROM partner_invites WHERE from_user = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1", (from_user,))
        existing = await cur.fetchone()
        await db.commit()
        if existing:
            return existing[0]
    raise RuntimeError("Could not create or retrieve a pending partner invite")


class _InviteRejected(Exception):
    """Внутренний control-flow для accept_invite — прерывает транзакцию (rollback)."""
    def __init__(self, reason: str):
        self.reason = reason


async def accept_invite(token: str, accepting_user: int) -> dict:
    """Принять приглашение. reason: invalid | self | inviter_taken | already_paired | ok.

    Атомарно на одном соединении/транзакции (важно на 2+ Fly-машинах): проверка
    "уже в паре" — это не отдельный SELECT перед INSERT (гонка), а сам INSERT ...
    ON CONFLICT DO NOTHING RETURNING — если у user_id уже есть партнёр (PRIMARY
    KEY), вставка молча не пройдёт и мы это увидим по пустому RETURNING.
    """
    try:
        async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "UPDATE partner_invites SET status='accepted' WHERE token = ? AND status = 'pending' "
                "RETURNING from_user", (token,))
            row = await cur.fetchone()
            if not row:
                raise _InviteRejected("invalid")
            from_user = row["from_user"]
            if from_user == accepting_user:
                raise _InviteRejected("self")

            now = _now()
            cur1 = await db.execute(
                "INSERT INTO partners (user_id, partner_id, since) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO NOTHING RETURNING user_id",
                (from_user, accepting_user, now))
            if not await cur1.fetchone():
                raise _InviteRejected("inviter_taken")
            cur2 = await db.execute(
                "INSERT INTO partners (user_id, partner_id, since) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO NOTHING RETURNING user_id",
                (accepting_user, from_user, now))
            if not await cur2.fetchone():
                raise _InviteRejected("already_paired")

            # Neither user may retain a pre-pair invite that could unexpectedly
            # be accepted after the pair is later dissolved.
            await db.execute(
                "DELETE FROM partner_invites WHERE from_user IN (?, ?) AND status = 'pending'",
                (from_user, accepting_user))
            await db.commit()
            return {"ok": True, "partner_id": from_user}
    except _InviteRejected as e:
        return {"ok": False, "reason": e.reason}


async def unpair(user_id: int) -> None:
    """Разорвать только текущую симметричную пару пользователя.

    Поиск партнёра и удаление выполняются в одной транзакции. Поэтому
    запоздалый запрос не может удалить новую пару бывшего партнёра.
    """
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute("SELECT partner_id FROM partners WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            partner_id = row[0]
            await db.execute(
                "DELETE FROM partners "
                "WHERE (user_id = ? AND partner_id = ?) OR (user_id = ? AND partner_id = ?)",
                (user_id, partner_id, partner_id, user_id))
        # Calling unpair while not paired also cancels the caller's pending invite.
        # We intentionally do not touch another user's newer invite or relationship.
        await db.execute(
            "DELETE FROM partner_invites WHERE from_user = ? AND status = 'pending'", (user_id,))
        await db.commit()


async def get_pair(user_id: int) -> dict | None:
    """Партнёр + момент создания пары (since) — граница «пар-периода»."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT partner_id, since FROM partners WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return {"partner_id": row["partner_id"], "since": row["since"]} if row else None


async def sync_film_to_partner(user_id: int, film_id: int) -> None:
    """Синхрон «только новые»: если есть партнёр — добавить фильм ему в «Хочу»
    (идемпотентно; существующие у партнёра фильмы не трогаем)."""
    partner = await get_partner(user_id)
    if partner is not None:
        await add_to_list(partner, film_id, "want_to_watch")


async def pair_period_stats(user_id: int, partner_id: int, since: str) -> dict:
    """Статистика ПАРЫ по фильмам пар-периода (оба добавили после since).
    Формат как личная статистика + поля совместимости. Просмотрено = оба
    посмотрели; оценки/гистограмма = оценки обоих; жанры/актёры — из оба-просмотренных."""
    year_now = datetime.now(timezone.utc).year
    like = f"{year_now}-%"
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """
            SELECT f.id AS film_id, f.genres, f.actors, f.directors, f.runtime, f.title, f.poster_url,
                   a.status AS sa, a.rating AS ra, a.watched_at AS wa,
                   b.status AS sb, b.rating AS rb, b.watched_at AS wb
            FROM user_films a
            JOIN user_films b ON a.film_id = b.film_id
            JOIN films f ON f.id = a.film_id
            WHERE a.user_id = ? AND b.user_id = ? AND a.added_at >= ? AND b.added_at >= ?
            """, (user_id, partner_id, since, since))).fetchall()

    both_watched = [r for r in rows if r["sa"] == "watched" and r["sb"] == "watched"]
    watched = len(both_watched)
    want = len(rows) - watched

    pooled = [r["ra"] for r in rows if r["ra"] is not None] + [r["rb"] for r in rows if r["rb"] is not None]
    avg_rating = round(sum(pooled) / len(pooled), 1) if pooled else None
    dist = {i: 0 for i in range(1, 11)}
    for v in pooled:
        dist[v] = dist.get(v, 0) + 1
    rating_dist = [dist[i] for i in range(1, 11)]

    genre_counts, actor_counts, director_counts = {}, {}, {}
    total_min = 0
    year_min, year_genre, year_actor = 0, {}, {}
    year_ratings, year_count = [], 0
    for r in both_watched:
        m = re.search(r"\d+", r["runtime"] or "")
        rt = int(m.group(0)) if m else 0
        total_min += rt
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
        if (r["wa"] or "").startswith(str(year_now)) or (r["wb"] or "").startswith(str(year_now)):
            year_count += 1
            year_min += rt
            for g in (r["genres"] or "").split(","):
                g = g.strip()
                if g and g != "N/A":
                    year_genre[g] = year_genre.get(g, 0) + 1
            for a in (r["actors"] or "").split(","):
                a = a.strip()
                if a:
                    year_actor[a] = year_actor.get(a, 0) + 1
            for rv in (r["ra"], r["rb"]):
                if rv is not None:
                    year_ratings.append(rv)

    total_refs = sum(genre_counts.values())
    top_genres_pct = [(g, round(c / total_refs * 100)) for g, c in sorted(genre_counts.items(), key=lambda x: -x[1])[:5]] if total_refs else []
    top_actors = [(n, c) for n, c in sorted(actor_counts.items(), key=lambda x: -x[1]) if c >= 2][:5]
    top_directors = [(n, c) for n, c in sorted(director_counts.items(), key=lambda x: -x[1]) if c >= 2][:3]

    # Совместимость по фильмам пар-периода, которые оценили ОБА.
    rated = [{"film_id": r["film_id"], "a": r["ra"], "b": r["rb"], "title": r["title"], "poster_url": r["poster_url"]}
             for r in rows if r["ra"] is not None and r["rb"] is not None]
    agreement = matches = None
    controversial = best = None
    common_favorites = []
    disagreements = []
    if rated:
        diffs = [abs(item["a"] - item["b"]) for item in rated]
        agreement = round(100 - (sum(diffs) / len(rated)) / 9 * 100)
        matches = sum(1 for item in rated if item["a"] == item["b"])
        ranked_favorites = sorted(rated, key=lambda item: (item["a"] + item["b"], item["a"], item["b"]), reverse=True)
        common_favorites = [{"film_id": item["film_id"], "title": item["title"], "poster_url": item["poster_url"],
                             "avg": round((item["a"] + item["b"]) / 2, 1)} for item in ranked_favorites[:3]]
        ranked_disagreements = [item for item in sorted(rated, key=lambda item: abs(item["a"] - item["b"]), reverse=True)
                                if item["a"] != item["b"]]
        disagreements = [{"film_id": item["film_id"], "title": item["title"], "poster_url": item["poster_url"],
                          "a": item["a"], "b": item["b"], "diff": abs(item["a"] - item["b"])}
                         for item in ranked_disagreements[:3]]
        top_dispute = disagreements[0] if disagreements else None
        controversial = top_dispute
        top_favorite = common_favorites[0] if common_favorites else None
        best = top_favorite

    ranked = sorted(year_actor.items(), key=lambda x: -x[1])
    year_top_actor = ranked[0] if ranked and ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]) else None
    year = {
        "year": year_now, "count": year_count, "total_runtime_min": year_min,
        "top_genre": max(year_genre.items(), key=lambda x: x[1])[0] if year_genre else None,
        "top_actor": year_top_actor,
        "avg_rating": round(sum(year_ratings) / len(year_ratings), 1) if year_ratings else None,
        "best_avg": None, "best_titles": [],
    }

    return {
        "watched": watched, "want": want,
        "avg_rating": avg_rating, "rating_count": len(pooled), "rating_dist": rating_dist,
        "total_runtime_min": total_min,
        "top_genres_pct": top_genres_pct, "top_actors": top_actors, "top_directors": top_directors,
        "year": year,
        "agreement": agreement, "rated_together": len(rated),
        "matches": matches, "controversial": controversial, "best": best,
        "common_favorites": common_favorites, "disagreements": disagreements,
    }


# ── Подборки (кураторские коллекции) ───────────────────────────────────────────
async def get_user_role(user_id: int) -> str | None:
    """Роль из БД (назначается вручную админом) — отдельно от ADMIN_USER_IDS (main.py)."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute("SELECT role FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def list_collections() -> list[dict]:
    """Все подборки: id, title, film_count, cover (постер первого добавленного фильма)."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT c.id, c.title,
                   (SELECT COUNT(*) FROM collection_films WHERE collection_id = c.id) AS film_count,
                   (SELECT f.poster_url FROM collection_films cf JOIN films f ON f.id = cf.film_id
                    WHERE cf.collection_id = c.id ORDER BY cf.added_at ASC LIMIT 1) AS cover
            FROM collections c
            ORDER BY c.created_at DESC
        """)
        return [dict(r) for r in await cur.fetchall()]


async def create_collection(title: str, created_by: int) -> int:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "INSERT INTO collections (title, created_by, created_at) VALUES (?,?,?) RETURNING id",
            (title, created_by, _now()))
        row = await cur.fetchone()
        await db.commit()
        return row["id"]


async def delete_collection(collection_id: int) -> None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute("DELETE FROM collection_films WHERE collection_id = ?", (collection_id,))
        await db.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        await db.commit()


async def get_collection(collection_id: int) -> dict | None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, title FROM collections WHERE id = ?", (collection_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def add_film_to_collection(collection_id: int, film_id: int) -> bool:
    """True — фильм реально добавлен (не был в подборке раньше)."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "INSERT INTO collection_films (collection_id, film_id, added_at) VALUES (?,?,?) "
            "ON CONFLICT(collection_id, film_id) DO NOTHING RETURNING film_id",
            (collection_id, film_id, _now()))
        row = await cur.fetchone()
        await db.commit()
        return row is not None


async def remove_film_from_collection(collection_id: int, film_id: int) -> None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "DELETE FROM collection_films WHERE collection_id = ? AND film_id = ?",
            (collection_id, film_id))
        await db.commit()


async def get_collection_films(collection_id: int, user_id: int) -> list[dict]:
    """Фильмы подборки в порядке добавления куратором — тот же формат, что browse_*
    (community-рейтинг + мой статус), чтобы фронтенд переиспользовал posterTile()."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT {_BROWSE_COLS}
            FROM films f
            JOIN collection_films cf ON cf.film_id = f.id AND cf.collection_id = ?
            LEFT JOIN user_films me ON me.film_id = f.id AND me.user_id = ?
            ORDER BY cf.added_at ASC
            """, (collection_id, user_id))
        return [_browse_dict(r) for r in await cur.fetchall()]
