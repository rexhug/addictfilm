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
import logging
import os
import re
import secrets
import aiosqlite
import db_runtime
from datetime import datetime, timezone, timedelta
from config import DATABASE_URL

logger = logging.getLogger(__name__)

# SQLite локально (DB_PATH=movies.db рядом с проектом); в облаке — Postgres (Neon)
# через DATABASE_URL, см. db_runtime.py. DB_PATH используется только для SQLite-режима.
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "movies.db"))
_PG = db_runtime.uses_postgres(DATABASE_URL)
_GENRES_CACHE_TTL_SECONDS = max(30, int(os.getenv("GENRES_CACHE_TTL_SEC", "300")))
_genres_cache: tuple[float, list[dict]] | None = None

# A migration record is deliberately stored in the database, not in a deploy
# variable.  It lets an operator answer "which schema is this instance on?"
# and makes every future data/schema change an explicit, auditable step.
_SCHEMA_MIGRATION_LEGACY_COLUMNS = "2026-07-25-legacy-columns"
_SCHEMA_MIGRATION_DIRECTOR_PHOTOS = "2026-07-25-director-photos"
_SCHEMA_MIGRATION_PERSON_PORTRAIT_RETRY = "2026-07-25-person-portrait-retry"
_SCHEMA_MIGRATION_PERSON_PORTRAIT_COMPLETENESS = "2026-07-25-person-portrait-completeness"
_SCHEMA_MIGRATION_SEARCH_TEXT_DIRECTORS = "2026-07-26-search-text-directors"
_SCHEMA_MIGRATION_PAIR_NOTIFICATIONS = "2026-07-26-pair-notifications"
_SCHEMA_MIGRATION_PAIR_NOTIFICATION_EVENTS = "2026-07-26-pair-notification-event-model"
_SCHEMA_MIGRATION_PAIR_NOTIFICATION_OUTBOX = "2026-07-26-pair-notification-outbox"
_SCHEMA_MIGRATION_PAIR_HISTORY = "2026-07-26-pair-history"
_SCHEMA_MIGRATION_RECOMMENDATIONS = "2026-07-26-recommendations-v1"
_SCHEMA_MIGRATION_RECOMMENDATION_RESULTS = "2026-07-26-recommendations-v2-results"
_PAIR_INVITE_TTL = timedelta(days=7)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Каталог наполняется из kinopoisk (жанры по-русски) и OMDb (по-английски) — из-за
# этого в film_genres встречаются дубли одного жанра на двух языках («драма» и
# «Drama»). Приводим к единому русскому канону: при записи (см. _split_genres) и
# при чтении (list_genres мёржит, browse_by_genre матчит все алиасы) — без миграции.
_GENRE_CANON: dict[str, str] = {
    "drama": "драма", "comedy": "комедия", "thriller": "триллер", "action": "боевик",
    "crime": "криминал", "mystery": "детектив", "detective": "детектив", "horror": "ужасы",
    "sci-fi": "фантастика", "science fiction": "фантастика", "fantasy": "фэнтези",
    "adventure": "приключения", "biography": "биография", "history": "история",
    "romance": "мелодрама", "melodrama": "мелодрама", "sport": "спорт", "war": "военный",
    "documentary": "документальный", "family": "семейный", "animation": "мультфильм",
    "cartoon": "мультфильм", "anime": "мультфильм", "аниме": "мультфильм",
    "детский": "семейный", "kids": "семейный", "children": "семейный",
    "western": "приключения", "вестерн": "приключения", "musical": "мюзикл",
    "music": "мюзикл", "музыка": "мюзикл", "film-noir": "криминал", "фильм-нуар": "криминал",
    "short": "короткометражка",
}

# Продуктовая витрина жанров: только осмысленные top-level жанры каталога.
# Форматы («короткометражка»), ТВ-мусор («реальное ТВ») и редкие хвосты
# («спорт», «мюзикл») в витрину не попадают — фильмы при этом остаются
# доступными через поиск и карточки. Сортировка по count делается в list_genres.
_GENRE_SHOWCASE = frozenset((
    "драма", "боевик", "комедия", "триллер", "приключения", "фантастика",
    "криминал", "детектив", "фэнтези", "ужасы", "мультфильм", "мелодрама",
    "семейный", "биография", "военный", "история", "документальный",
))


def _canon_genre(genre: str | None) -> str:
    """Единый (русский) канон жанра; неизвестные значения — как есть (обрезанные)."""
    s = (genre or "").strip()
    return _GENRE_CANON.get(s.casefold(), s)


def _genre_query_aliases(genre: str) -> list[str]:
    """Все варианты написания жанра (рус+англ, lower) для матча в film_genres."""
    canon = _canon_genre(genre)
    aliases = {canon.casefold(), (genre or "").strip().casefold()}
    aliases.update(en for en, ru in _GENRE_CANON.items() if ru == canon)
    return [a for a in aliases if a]


def _split_genres(value: str | None) -> list[str]:
    """Genre values for the derived, indexable film_genres table. Храним КАК ЕСТЬ
    (kinopoisk даёт рус., OMDb — англ.); к единому канону приводим только при
    чтении (list_genres мёржит, browse_by_genre матчит все алиасы) — без миграции
    и без риска задвоить счётчики при повторном бэкфилле проекции."""
    return list(dict.fromkeys(
        genre.strip() for genre in (value or "").split(",")
        if genre.strip() and genre.strip() != "N/A"
    ))


def _split_people(value: str | None) -> list[str]:
    """Return the canonical comma-separated person names stored on a film."""
    return list(dict.fromkeys(name.strip() for name in (value or "").split(",") if name.strip()))


def _person_key(name: str | None) -> str:
    """Stable, forgiving key for joining names from two catalog providers."""
    return re.sub(r"\s+", " ", str(name or "").strip()).casefold()


def _portrait_urls(entry: object) -> list[str]:
    """Read a primary portrait and its source fallbacks from one JSON entry."""
    if not isinstance(entry, dict):
        return []
    values = [entry.get("photo_url"), *(entry.get("fallback_photo_urls") or [])]
    result: list[str] = []
    for value in values:
        url = str(value or "").strip()
        if url.startswith(("https://", "http://")) and url not in result:
            result.append(url)
    return result


def _portrait_entries(value: str | None) -> list[dict]:
    """Tolerantly decode the stored people portrait payload."""
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [entry for entry in decoded if isinstance(entry, dict)] if isinstance(decoded, list) else []


async def _add_column_if_missing(table: str, col_def: str) -> None:
    """ALTER TABLE ADD COLUMN, идемпотентно. Каждая попытка — В СВОЁМ соединении/
    транзакции: под Postgres любая ошибка (например «колонка уже есть») переводит
    ВСЮ транзакцию в aborted-состояние — если бы это было внутри общего блока
    init_db(), она утянула бы за собой все последующие CREATE TABLE IF NOT EXISTS."""
    try:
        async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - drivers expose different exception classes.
        # Fresh databases call this only after the base tables are created.  On
        # existing databases the only harmless failure is "already exists".
        # Do not hide syntax, permission, disk, or connectivity errors: those
        # used to leave an instance on a partially upgraded schema unnoticed.
        message = str(exc).lower()
        duplicate_markers = ("duplicate column", "already exists", "duplicate key")
        if any(marker in message for marker in duplicate_markers):
            return
        logger.exception("Schema migration failed while adding %s.%s", table, col_def)
        raise


async def _schema_migration_applied(version: str) -> bool:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        row = await (await db.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
        )).fetchone()
        return row is not None


async def _record_schema_migration(version: str) -> None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?) "
            "ON CONFLICT(version) DO NOTHING",
            (version, _now()),
        )
        await db.commit()


async def _apply_legacy_column_migration() -> None:
    """Upgrade databases created before the current film/user columns existed."""
    for table, col_def in (
        ("films", "backdrop_url TEXT"),
        ("films", "age_rating TEXT"),
        ("films", "actors_photos TEXT"),
        ("films", "kp_id TEXT"),
        ("films", "search_text TEXT"),
        ("films", "poster_checked_at TEXT"),
        ("films", "artwork_checked_at TEXT"),
        ("films", "actor_photos_checked_at TEXT"),
        ("users", "role TEXT"),
        ("users", "photo_url TEXT"),
    ):
        await _add_column_if_missing(table, col_def)


async def _apply_director_photo_migration() -> None:
    """Add a separate, cacheable portrait map for directors.

    It deliberately mirrors ``actors_photos`` instead of overloading it: a
    person can be both an actor and a director, and stats need the role that
    is shown in the UI to be explicit.
    """
    for table, col_def in (
        ("films", "directors_photos TEXT"),
        ("films", "director_photos_checked_at TEXT"),
    ):
        await _add_column_if_missing(table, col_def)


async def _apply_person_portrait_retry_migration() -> None:
    """Allow one immediate repair pass for portraits blanked by the first rollout.

    The original migration correctly remembered a completed Wikidata lookup,
    but a transient Commons miss then stayed blank forever.  Resetting only
    rows with no stored URL is data-safe: existing working portraits are never
    touched, and the new 14-day retry guard prevents repeated work afterwards.
    """
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        for photos, checked in (
            ("actors_photos", "actor_photos_checked_at"),
            ("directors_photos", "director_photos_checked_at"),
        ):
            await db.execute(
                f"UPDATE films SET {checked} = NULL WHERE {photos} IS NULL OR {photos} = '' "
                f"OR {photos} NOT LIKE '%https://%'"
            )
        await db.commit()


async def _apply_person_portrait_completeness_migration() -> None:
    """Retry partially populated portrait payloads once after the source merge.

    A row may contain one valid portrait and several people without one.  The
    first repair migration intentionally skipped it because it only detected
    completely blank JSON.  Resetting just these incomplete payloads lets the
    new merger attach a second source, while the normal 14-day guard keeps the
    public profile path calm afterwards.
    """
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        for photos, checked in (
            ("actors_photos", "actor_photos_checked_at"),
            ("directors_photos", "director_photos_checked_at"),
        ):
            await db.execute(
                f"UPDATE films SET {checked} = NULL WHERE {photos} LIKE '%\"photo_url\": null%' "
                f"OR {photos} LIKE '%\"photo_url\":null%'"
            )
        await db.commit()


async def _apply_search_text_directors_migration() -> None:
    """Перестроить lookup-текст каталога, добавив режиссёров: поиск по режиссёрам
    начинает работать наравне с актёрами/названиями. search_text — производная
    колонка, полностью пересобираемая из полей фильма, так что перезапись безопасна."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id, title, title_original, actors, directors, imdb_id, kp_id FROM films")).fetchall()
        for row in rows:
            await db.execute(
                "UPDATE films SET search_text = ? WHERE id = ?",
                (_catalog_search_text(row["title"], row["title_original"], row["actors"],
                                      row["directors"], row["imdb_id"], row["kp_id"]), row["id"]))
        await db.commit()


async def _apply_pair_notifications_migration() -> None:
    """Create the durable notification outbox and upgrade pair invitations.

    Notifications are persisted before a best-effort Telegram Bot API send.  The
    app can therefore always show a reliable in-app inbox even when a user has
    blocked the bot or Telegram is temporarily unavailable.
    """
    await _add_column_if_missing("partner_invites", "expires_at TEXT")
    notification_id = "SERIAL PRIMARY KEY" if _PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_notification_settings (
                user_id BIGINT PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'ru',
                telegram_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pair_invite_recipients (
                invite_token TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (invite_token, user_id),
                FOREIGN KEY (invite_token) REFERENCES partner_invites(token),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS notifications (
                id {notification_id},
                event_type TEXT NOT NULL,
                recipient_id BIGINT NOT NULL,
                actor_id BIGINT,
                entity_id TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{{}}',
                deep_link TEXT,
                read_at TEXT,
                created_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                FOREIGN KEY (recipient_id) REFERENCES users(id),
                FOREIGN KEY (actor_id) REFERENCES users(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                notification_id INTEGER NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                PRIMARY KEY (notification_id, channel),
                FOREIGN KEY (notification_id) REFERENCES notifications(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications(recipient_id, id DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(recipient_id, read_at, id DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_invite_recipients_user ON pair_invite_recipients(user_id, invite_token)")
        await db.commit()


async def _apply_pair_notification_event_model_migration() -> None:
    """Add canonical event records and remove old duplicate invite notices."""
    await _add_column_if_missing("partner_invites", "accepted_by_user_id BIGINT")
    await _add_column_if_missing("notifications", "event_id TEXT")
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pair_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                invitation_id TEXT,
                pair_id TEXT,
                inviter_user_id BIGINT,
                invitee_user_id BIGINT,
                actor_user_id BIGINT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (inviter_user_id) REFERENCES users(id),
                FOREIGN KEY (invitee_user_id) REFERENCES users(id),
                FOREIGN KEY (actor_user_id) REFERENCES users(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pair_event_recipients (
                event_id TEXT NOT NULL,
                recipient_user_id BIGINT NOT NULL,
                partner_user_id BIGINT,
                dispatched_at TEXT,
                PRIMARY KEY (event_id, recipient_user_id),
                FOREIGN KEY (event_id) REFERENCES pair_events(id) ON DELETE CASCADE,
                FOREIGN KEY (recipient_user_id) REFERENCES users(id),
                FOREIGN KEY (partner_user_id) REFERENCES users(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pair_events_invitation ON pair_events(invitation_id, created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pair_event_recipients_recipient ON pair_event_recipients(recipient_user_id, event_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_notifications_event ON notifications(event_id, recipient_id)")
        # Before canonical events, merely opening an invite generated a second
        # notification beside the invite card itself.  It was never a meaningful
        # completed event, so remove it together with its outbox rows.
        await db.execute("DELETE FROM notification_deliveries WHERE notification_id IN (SELECT id FROM notifications WHERE event_type = 'pair.invite.created')")
        await db.execute("DELETE FROM notifications WHERE event_type = 'pair.invite.created'")
        await db.commit()


async def _apply_pair_notification_outbox_migration() -> None:
    """Allow a restarted process to resume event delivery exactly once."""
    await _add_column_if_missing("pair_event_recipients", "dispatched_at TEXT")


async def _apply_pair_history_migration() -> None:
    """Persist pair-specific sessions without changing personal film data.

    ``partners`` intentionally remains the small current-state lookup table.
    This archive records only the intervals for one exact pair of Telegram IDs,
    so reuniting with the same person restores their history while a new partner
    always starts with a separate history.
    """
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pair_sessions (
                id TEXT PRIMARY KEY,
                pair_key TEXT NOT NULL,
                first_user_id BIGINT NOT NULL,
                second_user_id BIGINT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (first_user_id) REFERENCES users(id),
                FOREIGN KEY (second_user_id) REFERENCES users(id),
                CHECK (first_user_id < second_user_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pair_sessions_pair ON pair_sessions(pair_key, started_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pair_sessions_active ON pair_sessions(pair_key, ended_at)")

        # The prior schema kept only active links.  Preserve every relationship
        # that is active at migration time from its original ``since`` value.
        rows = await (await db.execute(
            "SELECT user_id, partner_id, since FROM partners WHERE user_id < partner_id"
        )).fetchall()
        for first_user_id, second_user_id, started_at in rows:
            first_user_id, second_user_id = int(first_user_id), int(second_user_id)
            pair_key = _pair_key(first_user_id, second_user_id)
            await db.execute(
                "INSERT INTO pair_sessions (id, pair_key, first_user_id, second_user_id, started_at, ended_at, created_at) "
                "VALUES (?,?,?,?,?,NULL,?) ON CONFLICT(id) DO NOTHING",
                (_legacy_pair_session_id(pair_key, started_at), pair_key, first_user_id, second_user_id,
                 started_at, started_at))

        # Events introduced with the notification outbox contain the exact
        # beginning of a pair in pair_id.  If such a pair already ended before
        # this archive ships, retain that recent history too.
        ended_events = await (await db.execute(
            "SELECT id, pair_id, created_at FROM pair_events "
            "WHERE event_type = 'pair.ended' AND pair_id LIKE 'pair:%'"
        )).fetchall()
        for event_id, pair_id, ended_at in ended_events:
            parsed = _parse_legacy_pair_id(pair_id)
            if not parsed:
                continue
            first_user_id, second_user_id, started_at = parsed
            pair_key = _pair_key(first_user_id, second_user_id)
            await db.execute(
                "INSERT INTO pair_sessions (id, pair_key, first_user_id, second_user_id, started_at, ended_at, created_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (f"legacy-ended:{event_id}", pair_key, first_user_id, second_user_id,
                 started_at, ended_at, started_at))
        await db.commit()


async def _apply_recommendations_migration() -> None:
    """Persistence for deterministic recommendations.

    It is intentionally separate from personal lists: a rejection must not
    mutate somebody's watch history, while a recommendation session can safely
    expire or be restarted without losing any personal data.
    """
    history_id = "SERIAL PRIMARY KEY" if _PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recommendation_sessions (
                id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                version TEXT NOT NULL,
                answers TEXT NOT NULL DEFAULT '{}',
                current_question TEXT,
                state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'complete', 'expired')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_recommendation_sessions_user ON recommendation_sessions(user_id, state, updated_at)")
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS recommendation_history (
                id {history_id},
                user_id BIGINT NOT NULL,
                film_id INTEGER NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('random', 'quiz')),
                session_id TEXT,
                role TEXT,
                score REAL,
                action TEXT NOT NULL DEFAULT 'shown' CHECK (action IN ('shown', 'opened', 'want', 'watched', 'rejected', 'another')),
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (film_id) REFERENCES films(id),
                FOREIGN KEY (session_id) REFERENCES recommendation_sessions(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_recommendation_history_user_film ON recommendation_history(user_id, film_id, action, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_recommendation_history_user_recent ON recommendation_history(user_id, created_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS film_recommendation_tags (
                film_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                confidence REAL NOT NULL,
                source TEXT NOT NULL,
                version TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (film_id, tag, source, version),
                FOREIGN KEY (film_id) REFERENCES films(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_recommendation_tags_tag ON film_recommendation_tags(tag, confidence)")
        await db.commit()


async def _apply_recommendation_results_migration() -> None:
    """Keep one completed quiz stable when the user reopens its results."""
    await _add_column_if_missing("recommendation_sessions", "results TEXT")


async def _run_schema_migrations() -> None:
    """Run ordered, idempotent schema upgrades and journal them on success."""
    migrations = (
        (_SCHEMA_MIGRATION_LEGACY_COLUMNS, _apply_legacy_column_migration),
        (_SCHEMA_MIGRATION_DIRECTOR_PHOTOS, _apply_director_photo_migration),
        (_SCHEMA_MIGRATION_PERSON_PORTRAIT_RETRY, _apply_person_portrait_retry_migration),
        (_SCHEMA_MIGRATION_PERSON_PORTRAIT_COMPLETENESS, _apply_person_portrait_completeness_migration),
        (_SCHEMA_MIGRATION_SEARCH_TEXT_DIRECTORS, _apply_search_text_directors_migration),
        (_SCHEMA_MIGRATION_PAIR_NOTIFICATIONS, _apply_pair_notifications_migration),
        (_SCHEMA_MIGRATION_PAIR_NOTIFICATION_EVENTS, _apply_pair_notification_event_model_migration),
        (_SCHEMA_MIGRATION_PAIR_NOTIFICATION_OUTBOX, _apply_pair_notification_outbox_migration),
        (_SCHEMA_MIGRATION_PAIR_HISTORY, _apply_pair_history_migration),
        (_SCHEMA_MIGRATION_RECOMMENDATIONS, _apply_recommendations_migration),
        (_SCHEMA_MIGRATION_RECOMMENDATION_RESULTS, _apply_recommendation_results_migration),
    )
    for version, migration in migrations:
        if await _schema_migration_applied(version):
            continue
        await migration()
        await _record_schema_migration(version)


# ── Инициализация ────────────────────────────────────────────────────────────
async def init_db() -> None:
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
                kp_id          TEXT,
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
                directors_photos TEXT, -- JSON-массив name/photo_url под тех же режиссёров, что в directors
                search_text    TEXT NOT NULL DEFAULT '',
                poster_checked_at TEXT,
                artwork_checked_at TEXT,
                actor_photos_checked_at TEXT,
                director_photos_checked_at TEXT,
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

        # Journal each completed schema step.  Base tables must be present
        # before legacy ALTER TABLE migrations are allowed to run.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        await db.commit()

        # Each legacy ALTER runs in an isolated transaction: PostgreSQL marks
        # a transaction failed after an expected duplicate-column error.
        await _run_schema_migrations()

        # Индексы под горячие запросы: список юзера и community-агрегат по фильму.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_user ON user_films(user_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_film ON user_films(film_id)")
        # Покрывают реальные ORDER BY списков, не меняя схему или данные.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_user_added ON user_films(user_id, status, added_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_user_watched ON user_films(user_id, status, watched_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_uf_user_top ON user_films(user_id, status, rating DESC, watched_at DESC)")
        # kp_id делает уже загруженный из Kinopoisk фильм доступным без повторного
        # запроса даже после рестарта. У старых synthetic kp_<id> он восстанавливается.
        await db.execute("UPDATE films SET kp_id = SUBSTR(imdb_id, 4) "
                         "WHERE kp_id IS NULL AND imdb_id LIKE 'kp_%'")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_films_kp_id "
                         "ON films(kp_id) WHERE kp_id IS NOT NULL")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_films_search_text ON films(search_text)")
        if _PG:
            # PostgreSQL needs pattern_ops for prefix LIKE to use a btree index
            # under non-C collations; SQLite uses GLOB below.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_films_search_text_prefix "
                "ON films (search_text text_pattern_ops)"
            )
        # Derived table: exact, indexed genre filtering instead of a full scan
        # through comma-separated films.genres on every browse request.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS film_genres (
                film_id INTEGER NOT NULL,
                genre TEXT NOT NULL,
                PRIMARY KEY (film_id, genre),
                FOREIGN KEY (film_id) REFERENCES films(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_film_genres_genre ON film_genres(genre, film_id)")
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
    await _backfill_catalog_search_text()
    await _backfill_film_genres()


async def ping() -> bool:
    """Лёгкая readiness-проверка доступности активной базы данных."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute("SELECT 1")
        return await cur.fetchone() is not None


# ── Постоянный кэш поиска ─────────────────────────────────────────────────────
async def search_cache_get(q: str, max_age_sec: int, empty_max_age_sec: int | None = None) -> list | None:
    """Fresh positive results, or a deliberately short-lived cached empty result."""
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
    try:
        results = json.loads(row["results"])
    except json.JSONDecodeError:
        return None
    max_age = empty_max_age_sec if not results and empty_max_age_sec is not None else max_age_sec
    return results if age <= max_age else None


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
    """Регистрация/обновление любого пользователя Telegram без write-amplification.

    InitData приходит с каждым API запросом. Профиль обновляем сразу при реальном
    изменении, а last_seen — максимум раз в 15 минут, поэтому обычная навигация
    больше не создаёт коммит в Postgres на каждый GET.
    """
    now = _now()
    last_seen_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
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
            WHERE users.first_name IS DISTINCT FROM excluded.first_name
               OR users.username IS DISTINCT FROM excluded.username
               OR (excluded.photo_url IS NOT NULL AND users.photo_url IS DISTINCT FROM excluded.photo_url)
               OR users.last_seen IS NULL OR users.last_seen < ?
            """,
            (user.get("id"), user.get("first_name"), user.get("username"), user.get("photo_url"), now, now,
             last_seen_cutoff),
        )
        await db.commit()


# ── Каталог фильмов ──────────────────────────────────────────────────────────
def _catalog_search_text(title: str | None, title_original: str | None,
                         actors: str | None, directors: str | None,
                         imdb_id: str, kp_id: str | None) -> str:
    """Unicode-safe lookup text stored once with a catalog record. Включает
    актёров и режиссёров — поиск по людям работает без внешних запросов."""
    values = (title, title_original, actors, directors, imdb_id, kp_id)
    return " ".join(" ".join(str(value or "").split()).casefold() for value in values).strip()


def _prefer_catalog_value(current: str | None, incoming: str | None) -> str | None:
    """Fill a genuinely missing catalog field without overwriting good data."""
    return current if str(current or "").strip() else incoming


def _prefer_richer_catalog_list(current: str | None, incoming: str | None,
                                splitter) -> str | None:
    """Keep the richer credit/genre list while avoiding provider churn."""
    current_items = splitter(current)
    incoming_items = splitter(incoming)
    if len(incoming_items) > len(current_items):
        return incoming
    return current if current_items else incoming


async def _backfill_catalog_search_text() -> None:
    """Give legacy catalog entries the same local-search behaviour as new ones."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id, title, title_original, actors, directors, imdb_id, kp_id FROM films "
            "WHERE search_text IS NULL OR search_text = ''")).fetchall()
        for row in rows:
            await db.execute(
                "UPDATE films SET search_text = ? WHERE id = ?",
                (_catalog_search_text(row["title"], row["title_original"], row["actors"],
                                      row["directors"], row["imdb_id"], row["kp_id"]), row["id"]))
        await db.commit()


async def _backfill_film_genres() -> None:
    """Populate the indexable genre projection for legacy catalog rows."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT id, genres FROM films")).fetchall()
        for row in rows:
            for genre in _split_genres(row["genres"]):
                await db.execute(
                    "INSERT INTO film_genres (film_id, genre) VALUES (?,?) ON CONFLICT DO NOTHING",
                    (row["id"], genre),
                )
        await db.commit()


async def _set_film_genres(db, film_id: int, genres: str | None) -> None:
    await db.execute("DELETE FROM film_genres WHERE film_id = ?", (film_id,))
    for genre in _split_genres(genres):
        await db.execute("INSERT INTO film_genres (film_id, genre) VALUES (?,?)", (film_id, genre))


def _invalidate_genres_cache() -> None:
    global _genres_cache
    _genres_cache = None


def _catalog_item(row) -> dict | None:
    """Catalog row in the normalized shape expected by the search UI."""
    imdb_id = str(row["imdb_id"] or "")
    kp_id = str(row["kp_id"] or "")
    if imdb_id.startswith("tt"):
        src, ref = "i", imdb_id
    elif kp_id:
        src, ref = "k", kp_id
    elif imdb_id.startswith("kp_"):
        src, ref = "k", imdb_id[3:]
    else:
        return None
    rating = row["imdb_rating"] or row["kp_rating"]
    return {
        "src": src,
        "ref": ref,
        "title": row["title"],
        "year": row["year"] or "?",
        "poster": row["poster_url"],
        "rating": rating,
        "genres": row["genres"],
        "type": "movie",
    }


async def search_catalog(query: str, limit: int = 8) -> list[dict]:
    """Search the permanent catalog before spending an external API request."""
    terms = [term.casefold() for term in query.split() if term]
    if not terms:
        return []

    def like(term: str) -> str:
        return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    clauses = ["search_text LIKE ? ESCAPE '\\'" for _ in terms]
    params = [like(term) for term in terms]
    exact = " ".join(terms)
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        safe_prefix = terms[0] if re.fullmatch(r"[\w-]+", terms[0], flags=re.UNICODE) else None
        rows = []
        # Most title searches start with the movie title. GLOB prefix can use
        # SQLite's idx_films_search_text; PostgreSQL uses LIKE + text_pattern_ops.
        # Fallback below preserves actor/infix discovery.
        if safe_prefix:
            prefix_clause = "search_text LIKE ? ESCAPE '\\'" if _PG else "search_text GLOB ?"
            prefix_value = safe_prefix + ("%" if _PG else "*")
            cur = await db.execute(
                "SELECT * FROM films WHERE " + prefix_clause + " AND " + " AND ".join(clauses) + " "
                "ORDER BY CASE WHEN search_text LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, id DESC LIMIT ?",
                (prefix_value, *params, like(exact), max(1, min(limit, 20))),
            )
            rows = await cur.fetchall()
        if len(rows) < max(1, min(limit, 20)):
            existing_ids = [row["id"] for row in rows]
            exclusion = "" if not existing_ids else " AND id NOT IN (" + ",".join("?" for _ in existing_ids) + ")"
            cur = await db.execute(
                "SELECT * FROM films WHERE " + " AND ".join(clauses) + exclusion + " "
                "ORDER BY CASE WHEN search_text LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, id DESC LIMIT ?",
                (*params, *existing_ids, like(exact), max(1, min(limit, 20)) - len(rows)),
            )
            rows += await cur.fetchall()
    return [item for row in rows if (item := _catalog_item(row)) is not None]


async def get_film_id_by_source(src: str, ref: str) -> int | None:
    """Find a cataloged film by the identifier sent back to the search UI."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        if src == "i":
            cur = await db.execute("SELECT id FROM films WHERE imdb_id = ?", (ref,))
        else:
            cur = await db.execute(
                "SELECT id FROM films WHERE kp_id = ? OR imdb_id = ? LIMIT 1",
                (ref, f"kp_{ref}"))
        row = await cur.fetchone()
        return row[0] if row else None


async def get_catalog_item_by_source(src: str, ref: str) -> dict | None:
    """Exact catalog lookup for a direct IMDb/Kinopoisk search."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        if src == "i":
            cur = await db.execute("SELECT * FROM films WHERE imdb_id = ?", (ref,))
        else:
            cur = await db.execute(
                "SELECT * FROM films WHERE kp_id = ? OR imdb_id = ? LIMIT 1",
                (ref, f"kp_{ref}"))
        row = await cur.fetchone()
    return _catalog_item(row) if row else None


async def get_or_create_film(
    imdb_id: str, title: str, year: str | None = None, genres: str | None = None,
    runtime: str | None = None, imdb_rating: str | None = None, imdb_votes: str | None = None,
    plot: str | None = None, poster_url: str | None = None, title_original: str | None = None,
    kp_rating: str | None = None, directors: str | None = None, actors: str | None = None,
    backdrop_url: str | None = None, age_rating: str | None = None, actors_photos: str | None = None,
    directors_photos: str | None = None, kp_id: str | None = None,
) -> int:
    """Возвращает id фильма в общем каталоге, создавая запись при первом появлении
    (dedup по imdb_id/kp_id). Идемпотентно — один фильм на всех пользователей."""
    kp_id = str(kp_id).strip() if kp_id else None
    search_text = _catalog_search_text(title, title_original, actors, directors, imdb_id, kp_id)
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, title, title_original, year, genres, directors, actors, runtime, "
            "imdb_rating, kp_rating, imdb_votes, plot, kp_id FROM films "
            "WHERE imdb_id = ? OR (kp_id IS NOT NULL AND kp_id = ?) LIMIT 1",
            (imdb_id, kp_id))
        row = await cur.fetchone()
        if row:
            merged_title = _prefer_catalog_value(row["title"], title)
            merged_original = _prefer_catalog_value(row["title_original"], title_original)
            merged_year = _prefer_catalog_value(row["year"], year)
            merged_genres = _prefer_richer_catalog_list(row["genres"], genres, _split_genres)
            merged_directors = _prefer_richer_catalog_list(row["directors"], directors, _split_people)
            merged_actors = _prefer_richer_catalog_list(row["actors"], actors, _split_people)
            merged_runtime = _prefer_catalog_value(row["runtime"], runtime)
            merged_imdb_rating = _prefer_catalog_value(row["imdb_rating"], imdb_rating)
            merged_kp_rating = _prefer_catalog_value(row["kp_rating"], kp_rating)
            merged_imdb_votes = _prefer_catalog_value(row["imdb_votes"], imdb_votes)
            merged_plot = _prefer_catalog_value(row["plot"], plot)
            merged_kp_id = _prefer_catalog_value(row["kp_id"], kp_id)
            merged_search_text = _catalog_search_text(
                merged_title, merged_original, merged_actors, merged_directors, imdb_id, merged_kp_id,
            )
            await db.execute(
                "UPDATE films SET title = ?, title_original = ?, year = ?, genres = ?, directors = ?, actors = ?, "
                "runtime = ?, imdb_rating = ?, kp_rating = ?, imdb_votes = ?, plot = ?, kp_id = ?, search_text = ?, "
                "poster_url = COALESCE(NULLIF(poster_url, ''), ?), "
                "backdrop_url = COALESCE(NULLIF(backdrop_url, ''), ?), "
                "age_rating = COALESCE(age_rating, ?), "
                "actors_photos = COALESCE(NULLIF(actors_photos, ''), ?), "
                "directors_photos = COALESCE(NULLIF(directors_photos, ''), ?) "
                "WHERE id = ?",
                (merged_title, merged_original, merged_year, merged_genres, merged_directors, merged_actors,
                 merged_runtime, merged_imdb_rating, merged_kp_rating, merged_imdb_votes, merged_plot,
                 merged_kp_id, merged_search_text, poster_url, backdrop_url, age_rating, actors_photos,
                 directors_photos, row["id"]))
            if merged_genres != row["genres"]:
                await _set_film_genres(db, row["id"], merged_genres)
            await db.commit()
            if merged_genres != row["genres"]:
                _invalidate_genres_cache()
            return row["id"]
        # RETURNING id вместо lastrowid — портируемо (SQLite 3.35+ и Postgres одинаково).
        cur = await db.execute(
            """
            INSERT INTO films
                (imdb_id, kp_id, title, title_original, year, genres, directors, actors, runtime,
                 imdb_rating, kp_rating, imdb_votes, plot, poster_url, backdrop_url, age_rating,
                 actors_photos, directors_photos, search_text, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (imdb_id, kp_id, title, title_original, year, genres, directors, actors, runtime,
             imdb_rating, kp_rating, imdb_votes, plot, poster_url, backdrop_url, age_rating,
             actors_photos, directors_photos, search_text, _now()),
        )
        inserted = await cur.fetchone()
        if inserted:
            await _set_film_genres(db, inserted["id"], genres)
        await db.commit()
        if inserted:
            _invalidate_genres_cache()
            return inserted["id"]
        # Гонка: кто-то вставил параллельно — перечитываем.
        cur = await db.execute(
            "SELECT id FROM films WHERE imdb_id = ? OR (kp_id IS NOT NULL AND kp_id = ?) LIMIT 1",
            (imdb_id, kp_id))
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


async def mark_film_artwork_checked(imdb_id: str, backdrop_url: str | None,
                                    age_rating: str | None = None) -> bool:
    """Persist a completed artwork lookup, including a legitimate empty result.

    Without this marker, films that simply have no backdrop would hit Kinopoisk
    every time someone opens the detail page.
    """
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE films SET backdrop_url = COALESCE(NULLIF(backdrop_url, ''), ?), "
            "age_rating = COALESCE(age_rating, ?), artwork_checked_at = ? "
            "WHERE imdb_id = ?",
            (backdrop_url, age_rating, _now(), imdb_id))
        await db.commit()
        return cur.rowcount > 0


async def mark_film_visuals_checked(
    imdb_id: str, poster_url: str | None, backdrop_url: str | None,
    age_rating: str | None = None,
) -> bool:
    """Persist one completed visual-asset lookup, including empty fields.

    A provider result without an image is valid data. Both timestamps prevent a
    film with no poster/backdrop from making the same provider call on every
    detail-page visit.
    """
    now = _now()
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE films SET poster_url = COALESCE(NULLIF(poster_url, ''), ?), "
            "backdrop_url = COALESCE(NULLIF(backdrop_url, ''), ?), "
            "age_rating = COALESCE(age_rating, ?), poster_checked_at = ?, artwork_checked_at = ? "
            "WHERE imdb_id = ?",
            (poster_url, backdrop_url, age_rating, now, now, imdb_id))
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


async def films_needing_actor_photo_enrichment(limit: int = 200) -> list[dict]:
    """Films not yet checked against the no-quota Wikidata/Commons cast source."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, imdb_id, actors, actors_photos FROM films "
            "WHERE imdb_id LIKE 'tt%' AND (actors IS NOT NULL AND actors <> '') "
            "AND actor_photos_checked_at IS NULL ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [dict(row) for row in await cur.fetchall()]


async def films_needing_director_photo_enrichment(limit: int = 200) -> list[dict]:
    """Films not yet checked against Wikidata/Commons for director portraits."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, imdb_id, directors, directors_photos FROM films "
            "WHERE imdb_id LIKE 'tt%' AND (directors IS NOT NULL AND directors <> '') "
            "AND director_photos_checked_at IS NULL ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [dict(row) for row in await cur.fetchall()]


async def watched_films_needing_people_photo_enrichment(user_id: int, people_column: str,
                                                         photo_column: str, checked_column: str, limit: int = 100,
                                                         retry_days: int = 14) -> list[dict]:
    """Return watched films with missing portraits, with a calm retry policy.

    Wikimedia can temporarily fail and some people receive a Commons portrait
    after the first lookup.  A one-time ``checked_at`` marker therefore must
    not turn a temporary blank into a permanent one.  We retry only portrait
    sets that still contain no URL, and no more often than every ``retry_days``.
    Column names are fixed by internal callers, never taken from the request.
    """
    if (people_column, photo_column, checked_column) not in {
        ("actors", "actors_photos", "actor_photos_checked_at"),
        ("directors", "directors_photos", "director_photos_checked_at"),
    }:
        raise ValueError("Unsupported person portrait columns")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retry_days)).isoformat()

    def incomplete(row: dict) -> bool:
        people = {_person_key(name) for name in _split_people(row.get(people_column))}
        if not people:
            return False
        covered = {
            _person_key(entry.get("name"))
            for entry in _portrait_entries(row.get(photo_column))
            if _portrait_urls(entry)
        }
        return not people.issubset(covered)

    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT f.id, f.imdb_id, f.{people_column}, f.{photo_column} "
            "FROM user_films uf JOIN films f ON f.id = uf.film_id "
            "WHERE uf.user_id = ? AND uf.status = 'watched' AND f.imdb_id LIKE 'tt%' "
            f"AND f.{people_column} IS NOT NULL AND f.{people_column} <> '' "
            f"AND (f.{checked_column} IS NULL OR f.{checked_column} < ?) "
            f"ORDER BY f.id DESC LIMIT ?",
            # Inspect a bounded superset in Python. JSON support differs
            # between SQLite and Postgres, whereas this exact-name check is
            # portable and catches a *partially* blank list correctly.
            (user_id, cutoff, max(1, min(limit * 4, 400))),
        )
        return [row for row in (dict(item) for item in await cur.fetchall()) if incomplete(row)][:limit]


async def watched_films_needing_director_photo_enrichment(user_id: int, limit: int = 100) -> list[dict]:
    """Backward-compatible director wrapper for existing callers."""
    return await watched_films_needing_people_photo_enrichment(
        user_id, "directors", "directors_photos", "director_photos_checked_at", limit,
    )


async def watched_films_needing_actor_photo_enrichment(user_id: int, limit: int = 100) -> list[dict]:
    """Actor equivalent of the director portrait refresh."""
    return await watched_films_needing_people_photo_enrichment(
        user_id, "actors", "actors_photos", "actor_photos_checked_at", limit,
    )


def _merge_people_portraits(existing_people: str | None, existing_payload: str | None,
                            incoming_people: str | None, incoming_payload: str | None) -> tuple[str, str]:
    """Merge Wikidata with the first catalogue source instead of erasing it.

    Wikidata is the preferred, freely licensed portrait source, but it does not
    have P18 photos for every person.  The original implementation replaced a
    useful Kinopoisk portrait with a Wikidata entry containing ``null``.  Keep
    both URL candidates per exact name, and retain old people only when they
    contribute a real portrait.  This makes a failed primary request recover
    in the browser without inventing or scraping a random face.
    """
    existing_entries = _portrait_entries(existing_payload)
    incoming_entries = _portrait_entries(incoming_payload)
    existing_by_key = {_person_key(item.get("name")): item for item in existing_entries if _person_key(item.get("name"))}
    incoming_by_key = {_person_key(item.get("name")): item for item in incoming_entries if _person_key(item.get("name"))}

    names: list[str] = []
    ordered_keys: list[str] = []
    for name in [*_split_people(incoming_people), *_split_people(existing_people)]:
        key = _person_key(name)
        # Old, unmatched names without a usable portrait usually come from a
        # weaker provider and only create duplicate rows in statistics.
        if key in ordered_keys or (key not in incoming_by_key and not _portrait_urls(existing_by_key.get(key))):
            continue
        ordered_keys.append(key)
        names.append(name)

    merged: list[dict] = []
    for key, name in zip(ordered_keys, names):
        fresh = incoming_by_key.get(key, {})
        old = existing_by_key.get(key, {})
        urls: list[str] = []
        for entry in (fresh, old):
            for url in _portrait_urls(entry):
                if url not in urls:
                    urls.append(url)
        entry: dict[str, object] = {
            "name": str(fresh.get("name") or old.get("name") or name).strip(),
            "photo_url": urls[0] if urls else None,
            "source": str(fresh.get("source") or old.get("source") or "catalog"),
        }
        if len(urls) > 1:
            entry["fallback_photo_urls"] = urls[1:]
        merged.append(entry)
    return ", ".join(names), json.dumps(merged, ensure_ascii=False)


async def _set_film_people_from_wikidata(film_id: int, people_column: str, photo_column: str,
                                          checked_column: str, people: str, photos_json: str) -> bool:
    """Persist a source-merged people payload. Columns are fixed internal names."""
    if (people_column, photo_column, checked_column) not in {
        ("actors", "actors_photos", "actor_photos_checked_at"),
        ("directors", "directors_photos", "director_photos_checked_at"),
    }:
        raise ValueError("Unsupported person portrait columns")
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        current = await (await db.execute(
            f"SELECT {people_column}, {photo_column} FROM films WHERE id = ?", (film_id,)
        )).fetchone()
        if current is None:
            return False
        merged_people, merged_photos = _merge_people_portraits(
            current[people_column], current[photo_column], people, photos_json,
        )
        cur = await db.execute(
            f"UPDATE films SET {people_column} = ?, {photo_column} = ?, {checked_column} = ? WHERE id = ?",
            (merged_people, merged_photos, _now(), film_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def set_film_cast_from_wikidata(film_id: int, actors: str, photos_json: str) -> bool:
    """Persist researched cast while retaining working portraits from earlier sources."""
    return await _set_film_people_from_wikidata(
        film_id, "actors", "actors_photos", "actor_photos_checked_at", actors, photos_json,
    )


async def mark_film_actor_photos_checked(film_id: int) -> bool:
    """Remember even an empty result, so a film never triggers repeat lookups."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE films SET actor_photos_checked_at = ? WHERE id = ?",
            (_now(), film_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def set_film_directors_from_wikidata(film_id: int, directors: str, photos_json: str) -> bool:
    """Persist researched directors while retaining working portraits from earlier sources."""
    return await _set_film_people_from_wikidata(
        film_id, "directors", "directors_photos", "director_photos_checked_at", directors, photos_json,
    )


async def mark_film_director_photos_checked(film_id: int) -> bool:
    """Remember an empty director lookup to avoid repeated external queries."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE films SET director_photos_checked_at = ? WHERE id = ?",
            (_now(), film_id),
        )
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
            # Сортировки экрана «Смотрел». Ключи — из фиксированного словаря (без
            # инъекций). Неоценённые всегда после оценённых (rating IS NULL → 1),
            # вторичный ключ f.id — стабильный порядок при равных значениях.
            # «new/old» берут реальный watched_at (с фолбэком на added_at), НЕ год.
            orders = {
                "best":  "(uf.rating IS NULL), uf.rating DESC, uf.watched_at DESC, f.id DESC",
                "worst": "(uf.rating IS NULL), uf.rating ASC,  uf.watched_at DESC, f.id DESC",
                "new":   "COALESCE(uf.watched_at, uf.added_at) DESC, f.id DESC",
                "old":   "COALESCE(uf.watched_at, uf.added_at) ASC,  f.id ASC",
                # обратная совместимость со старыми значениями
                "rating": "(uf.rating IS NULL), uf.rating DESC, uf.watched_at DESC, f.id DESC",
                "date":   "COALESCE(uf.watched_at, uf.added_at) DESC, f.id DESC",
            }
            order = orders.get(sort, orders["date"])
            cur = await db.execute(
                base + f" AND uf.status='watched' ORDER BY {order} LIMIT ? OFFSET ?",
                (user_id, limit, offset))
        else:
            cur = await db.execute(
                base + " AND uf.status='want_to_watch' ORDER BY uf.added_at DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset))
        return [dict(r) for r in await cur.fetchall()]


def _person_column(role: str) -> str:
    """Map the public role enum to a fixed SQL column name."""
    if role == "actor":
        return "actors"
    if role == "director":
        return "directors"
    raise ValueError("Unknown person role")


def _films_for_person(rows, column: str, name: str) -> list[dict]:
    """Keep exact comma-separated name matches; never use a loose SQL LIKE.

    A substring query would make a request for e.g. ``Tom`` leak unrelated
    people named ``Tommy``.  The watched collection is deliberately bounded
    (200 rows) and this exact matching is portable across SQLite and Postgres.
    """
    target = name.strip().casefold()
    return [dict(row) for row in rows if target in {person.casefold() for person in _split_people(row[column])}]


async def get_person_watched_films(user_id: int, role: str, name: str, limit: int = 200) -> list[dict]:
    """Films this user watched that contain the selected actor or director."""
    column = _person_column(role)
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT f.*, uf.rating AS my_rating, uf.watched_at AS watched_at FROM user_films uf "
            "JOIN films f ON f.id = uf.film_id WHERE uf.user_id = ? AND uf.status = 'watched' "
            "ORDER BY uf.watched_at DESC LIMIT ?",
            (user_id, max(1, min(limit, 200))),
        )
        return _films_for_person(await cur.fetchall(), column, name)


async def get_pair_person_watched_films(user_id: int, partner_id: int, since: str,
                                         role: str, name: str, limit: int = 200) -> list[dict]:
    """Shared watched films for every saved session of this exact pair.

    The profile aggregate deliberately survives a reunion of the same two
    people.  The drill-down must use the identical membership rule; otherwise
    a card can say that a director appears in four shared films while tapping
    it opens only the films from the latest session.
    """
    column = _person_column(role)
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        sessions = await _pair_sessions_for(db, user_id, partner_id, since)
        period_sql, period_params = _pair_session_predicate(sessions)
        cur = await db.execute(
            "SELECT f.*, a.rating AS my_rating, b.rating AS partner_rating, "
            "a.watched_at AS watched_at FROM user_films a JOIN user_films b ON a.film_id = b.film_id "
            "JOIN films f ON f.id = a.film_id "
            "WHERE a.user_id = ? AND b.user_id = ? AND a.status = 'watched' AND b.status = 'watched' "
            f"AND ({period_sql}) ORDER BY a.watched_at DESC LIMIT ?",
            (user_id, partner_id, *period_params, max(1, min(limit, 200))),
        )
        return _films_for_person(await cur.fetchall(), column, name)


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
        count_row = await (await db.execute(
            "SELECT COUNT(*) FROM user_films WHERE user_id = ? AND status = 'want_to_watch'", (user_id,)
        )).fetchone()
        count = int(count_row[0]) if count_row else 0
        if not count:
            return None
        offset = secrets.randbelow(count)
        cur = await db.execute(
            """
            SELECT f.*, uf.rating AS my_rating FROM user_films uf JOIN films f ON f.id = uf.film_id
            WHERE uf.user_id = ? AND uf.status = 'want_to_watch'
            ORDER BY uf.added_at DESC LIMIT 1 OFFSET ?
            """, (user_id, offset))
        row = await cur.fetchone()
        return dict(row) if row else None


# ── Deterministic recommendations ───────────────────────────────────────────
_RECOMMENDATION_SESSION_TTL = timedelta(hours=12)


async def create_recommendation_session(user_id: int, version: str, session_id: str,
                                        current_question: str | None) -> dict:
    now = _now()
    expires = (datetime.now(timezone.utc) + _RECOMMENDATION_SESSION_TTL).isoformat()
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "INSERT INTO recommendation_sessions (id, user_id, version, answers, current_question, state, created_at, updated_at, expires_at) "
            "VALUES (?,?,?,?,?,'active',?,?,?)",
            (session_id, user_id, version, "{}", current_question, now, now, expires),
        )
        await db.commit()
    return {"id": session_id, "user_id": user_id, "version": version, "answers": {},
            "current_question": current_question, "state": "active", "created_at": now,
            "updated_at": now, "expires_at": expires}


async def get_recommendation_session(user_id: int, session_id: str) -> dict | None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM recommendation_sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
        row = await cur.fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["answers"] = json.loads(result.get("answers") or "{}")
        except (TypeError, json.JSONDecodeError):
            result["answers"] = {}
        try:
            result["results"] = json.loads(result.get("results") or "null")
        except (TypeError, json.JSONDecodeError):
            result["results"] = None
        if result["state"] == "active" and result["expires_at"] <= _now():
            await db.execute("UPDATE recommendation_sessions SET state='expired', updated_at=? WHERE id=?", (_now(), session_id))
            await db.commit()
            result["state"] = "expired"
        return result


async def update_recommendation_session(user_id: int, session_id: str, answers: dict,
                                        current_question: str | None, state: str = "active") -> dict | None:
    now = _now()
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE recommendation_sessions SET answers=?, current_question=?, state=?, results=NULL, updated_at=? "
            "WHERE id=? AND user_id=?",
            (json.dumps(answers, ensure_ascii=False, separators=(",", ":")), current_question, state, now, session_id, user_id),
        )
        await db.commit()
        if not cur.rowcount:
            return None
    return await get_recommendation_session(user_id, session_id)


async def restart_recommendation_session(user_id: int, session_id: str, current_question: str) -> dict | None:
    now = _now()
    expires = (datetime.now(timezone.utc) + _RECOMMENDATION_SESSION_TTL).isoformat()
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE recommendation_sessions SET answers='{}', current_question=?, state='active', results=NULL, updated_at=?, expires_at=? "
            "WHERE id=? AND user_id=?",
            (current_question, now, expires, session_id, user_id),
        )
        await db.commit()
        if not cur.rowcount:
            return None
    return await get_recommendation_session(user_id, session_id)


async def save_recommendation_session_results(user_id: int, session_id: str, items: list[dict]) -> dict | None:
    """Persist public result cards once; re-opening a completed quiz is stable."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE recommendation_sessions SET results=?, updated_at=? WHERE id=? AND user_id=? AND state='complete'",
            (json.dumps(items, ensure_ascii=False, separators=(",", ":")), _now(), session_id, user_id),
        )
        await db.commit()
        if not cur.rowcount:
            return None
    return await get_recommendation_session(user_id, session_id)


async def get_recommendation_candidates(user_id: int, partner_id: int | None = None,
                                        limit: int = 700) -> list[dict]:
    """Bounded local-catalog candidate pool with personal/pair state attached.

    All filters which matter for privacy and history are in SQL.  The scoring
    engine then deals only with a bounded set, never scans a public catalog.
    """
    limit = max(60, min(int(limit), 900))
    partner_id = int(partner_id) if partner_id is not None else None
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        if partner_id is None:
            cur = await db.execute(
                "SELECT f.*, mine.status AS my_status, mine.rating AS my_rating, "
                "NULL AS partner_status, NULL AS partner_rating, "
                "CASE WHEN mine.status='want_to_watch' THEN 1 ELSE 0 END AS in_wishlist "
                "FROM films f LEFT JOIN user_films mine ON mine.film_id=f.id AND mine.user_id=? "
                "WHERE COALESCE(TRIM(f.title), '') <> '' AND COALESCE(TRIM(f.poster_url), '') <> '' "
                "AND (mine.status IS NULL OR mine.status <> 'watched') "
                "AND NOT EXISTS (SELECT 1 FROM recommendation_history h WHERE h.user_id=? AND h.film_id=f.id AND h.action='rejected') "
                "AND NOT EXISTS (SELECT 1 FROM recommendation_history h WHERE h.user_id=? AND h.film_id=f.id AND h.action='shown' AND h.created_at >= ?) "
                "ORDER BY CAST(COALESCE(NULLIF(f.imdb_rating,''), NULLIF(f.kp_rating,''), '0') AS REAL) DESC, f.id DESC LIMIT ?",
                (user_id, user_id, user_id, recent_cutoff, limit),
            )
        else:
            cur = await db.execute(
                "SELECT f.*, mine.status AS my_status, mine.rating AS my_rating, "
                "partner.status AS partner_status, partner.rating AS partner_rating, "
                "CASE WHEN mine.status='want_to_watch' OR partner.status='want_to_watch' THEN 1 ELSE 0 END AS in_wishlist "
                "FROM films f "
                "LEFT JOIN user_films mine ON mine.film_id=f.id AND mine.user_id=? "
                "LEFT JOIN user_films partner ON partner.film_id=f.id AND partner.user_id=? "
                "WHERE COALESCE(TRIM(f.title), '') <> '' AND COALESCE(TRIM(f.poster_url), '') <> '' "
                "AND (mine.status IS NULL OR mine.status <> 'watched') "
                "AND (partner.status IS NULL OR partner.status <> 'watched') "
                "AND NOT EXISTS (SELECT 1 FROM recommendation_history h WHERE h.user_id IN (?,?) AND h.film_id=f.id AND h.action='rejected') "
                "AND NOT EXISTS (SELECT 1 FROM recommendation_history h WHERE h.user_id IN (?,?) AND h.film_id=f.id AND h.action='shown' AND h.created_at >= ?) "
                "ORDER BY CAST(COALESCE(NULLIF(f.imdb_rating,''), NULLIF(f.kp_rating,''), '0') AS REAL) DESC, f.id DESC LIMIT ?",
                (user_id, partner_id, user_id, partner_id, user_id, partner_id, recent_cutoff, limit),
            )
        return [dict(row) for row in await cur.fetchall()]


async def get_recommendation_preferences(user_id: int) -> dict:
    """Small, explainable signals from personal watch history, not opaque profiling."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT f.genres, f.actors, f.directors, uf.rating FROM user_films uf JOIN films f ON f.id=uf.film_id "
            "WHERE uf.user_id=? AND uf.status='watched' AND uf.rating IS NOT NULL ORDER BY uf.rated_at DESC LIMIT 160",
            (user_id,),
        )
        rows = [dict(row) for row in await cur.fetchall()]
    genre_scores: dict[str, float] = {}
    people_scores: dict[str, float] = {}
    for row in rows:
        rating = int(row.get("rating") or 0)
        signal = max(0, rating - 5)
        if not signal:
            continue
        for genre in _split_genres(row.get("genres")):
            key = _canon_genre(genre).casefold()
            genre_scores[key] = genre_scores.get(key, 0) + signal
        for person in _split_people(row.get("actors")) + _split_people(row.get("directors")):
            key = _person_key(person)
            people_scores[key] = people_scores.get(key, 0) + signal
    return {"genres": genre_scores, "people": people_scores, "count": len(rows)}


async def save_recommendation_tags(film_id: int, tags: dict[str, float], *, source: str = "rules", version: str = "v1") -> None:
    """Keep derived tags auditable; values are confidence, not claims about a film."""
    rows = [(film_id, tag, max(0.0, min(1.0, float(value))), source, version, _now()) for tag, value in tags.items()]
    if not rows:
        return
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        for row in rows:
            await db.execute(
                "INSERT INTO film_recommendation_tags (film_id, tag, confidence, source, version, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(film_id, tag, source, version) DO UPDATE SET "
                "confidence=excluded.confidence, updated_at=excluded.updated_at", row)
        await db.commit()


async def record_recommendation_history(user_id: int, film_id: int, mode: str, *, session_id: str | None = None,
                                        role: str | None = None, score: float | None = None,
                                        action: str = "shown") -> None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "INSERT INTO recommendation_history (user_id, film_id, mode, session_id, role, score, action, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, film_id, mode, session_id, role, score, action, _now()),
        )
        await db.commit()


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
def _person_photo_sources(rows, column: str) -> dict[str, list[str]]:
    """Collect ordered portrait candidates per exact person name.

    The first URL is shown first; the rest are delivered to the client as
    fallbacks.  This makes one flaky Commons edge node a visual inconvenience,
    not a permanently empty person card.
    """
    photos: dict[str, list[str]] = {}
    for row in rows:
        for entry in _portrait_entries(row[column]):
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            bucket = photos.setdefault(name, [])
            for photo_url in _portrait_urls(entry):
                if photo_url not in bucket:
                    bucket.append(photo_url)
    return photos


def _person_photo_map(rows, column: str) -> dict[str, str]:
    """Pick the preferred portrait per exact person name (legacy helper)."""
    return {name: urls[0] for name, urls in _person_photo_sources(rows, column).items() if urls}


def _actor_photo_map(rows) -> dict[str, str]:
    """Backward-compatible actor-specific helper for existing callers/tests."""
    return _person_photo_map(rows, "actors_photos")


def _person_credit_prominence(rows, column: str) -> dict[str, tuple[int, int]]:
    """Return (sum of credit positions, appearances) for each shown person.

    Both catalog providers return credits in billing order.  When two people
    appear in the same number of watched films, the one normally placed closer
    to the start of the credits is the best available, local popularity signal.
    It is deterministic, requires no third-party request, and works for old
    catalogue records too.
    """
    positions: dict[str, tuple[int, int]] = {}
    for row in rows:
        for index, name in enumerate(_split_people(row[column])):
            total, appearances = positions.get(name, (0, 0))
            positions[name] = (total + index, appearances + 1)
    return positions


def _rank_people(counts: dict[str, int], prominence: dict[str, tuple[int, int]]) -> list[tuple[str, int]]:
    """Sort by watched count first, then by how prominently credits list them."""
    def sort_key(item: tuple[str, int]) -> tuple[int, float, str]:
        name, count = item
        total, appearances = prominence.get(name, (10_000, 1))
        return (-count, total / max(1, appearances), name.casefold())
    return sorted(counts.items(), key=sort_key)


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
            SELECT f.genres, f.actors, f.actors_photos, f.directors, f.directors_photos, f.runtime
            FROM user_films uf JOIN films f ON f.id = uf.film_id
            WHERE uf.user_id = ? AND uf.status = 'watched'
            """, (user_id,))
        genre_counts, actor_counts, director_counts = {}, {}, {}
        total_runtime_min = 0
        watched_rows = await cur.fetchall()
        actor_photo_sources = _person_photo_sources(watched_rows, "actors_photos")
        director_photo_sources = _person_photo_sources(watched_rows, "directors_photos")
        actor_prominence = _person_credit_prominence(watched_rows, "actors")
        director_prominence = _person_credit_prominence(watched_rows, "directors")
        for r in watched_rows:
            for g in (r["genres"] or "").split(","):
                g = _canon_genre(g)
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
            # Keep the percentage for existing clients and include the exact
            # number of films for richer statistic cards.  A third tuple value
            # is backward compatible with clients that unpack only (genre, %).
            (g, round(c / total_refs * 100), c)
            for g, c in sorted(genre_counts.items(), key=lambda x: -x[1])[:5]
        ] if total_refs else []
        # Lists are fully aggregated in one query.  Count stays the primary
        # rank; when it is tied, billing prominence makes the rail feel like
        # a real film profile rather than an alphabetical dump.
        top_actors = [(n, c, *(actor_photo_sources.get(n) or [None]))
                      for n, c in _rank_people(actor_counts, actor_prominence)]
        top_directors = [(n, c, *(director_photo_sources.get(n) or [None]))
                         for n, c in _rank_people(director_counts, director_prominence)]

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
                g = _canon_genre(g)
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

_BROWSE_AGGREGATES = """
    WITH film_activity AS (
        SELECT film_id, AVG(rating) AS community_avg, COUNT(rating) AS community_count,
               COUNT(*) AS popularity
        FROM user_films
        GROUP BY film_id
    )
"""

_BROWSE_AGGREGATE_COLS = """
    f.*, COALESCE(a.community_avg, 0) AS community_avg,
    COALESCE(a.community_count, 0) AS community_count, COALESCE(a.popularity, 0) AS popularity,
    me.status AS my_status, me.rating AS my_rating
"""


async def browse_popular(user_id: int, limit: int = 30, offset: int = 0) -> list[dict]:
    """Популярное: по числу пользователей, добавивших фильм."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            _BROWSE_AGGREGATES + f"""
            SELECT {_BROWSE_AGGREGATE_COLS}
            FROM films f
            LEFT JOIN film_activity a ON a.film_id = f.id
            LEFT JOIN user_films me ON me.film_id = f.id AND me.user_id = ?
            ORDER BY COALESCE(a.popularity, 0) DESC, f.created_at DESC
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
            _BROWSE_AGGREGATES + """
            SELECT f.*, a.community_avg, a.community_count, a.popularity,
                   me.status AS my_status, me.rating AS my_rating
            FROM films f
            JOIN film_activity a ON a.film_id = f.id
            LEFT JOIN user_films me ON me.film_id = f.id AND me.user_id = ?
            WHERE a.community_count >= ?
            ORDER BY a.community_avg DESC, a.community_count DESC
            LIMIT ? OFFSET ?
            """, (user_id, mv, limit, offset))
        return [_browse_dict(r) for r in await cur.fetchall()]


async def browse_by_genre(user_id: int, genre: str, limit: int = 30, offset: int = 0) -> list[dict]:
    """Каталог по жанру, по популярности. Матчим все языковые алиасы жанра
    (напр. «драма» и «Drama»), чтобы русский канон собирал оба источника."""
    aliases = _genre_query_aliases(genre)
    placeholders = ",".join("?" for _ in aliases)
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            _BROWSE_AGGREGATES + f"""
            SELECT {_BROWSE_AGGREGATE_COLS}
            FROM films f
            LEFT JOIN film_activity a ON a.film_id = f.id
            LEFT JOIN user_films me ON me.film_id = f.id AND me.user_id = ?
            WHERE f.id IN (SELECT film_id FROM film_genres WHERE lower(genre) IN ({placeholders}))
            ORDER BY COALESCE(a.popularity, 0) DESC, f.created_at DESC
            LIMIT ? OFFSET ?
            """, (user_id, *aliases, limit, offset))
        return [_browse_dict(r) for r in await cur.fetchall()]


async def list_genres() -> list[dict]:
    """Жанры, присутствующие в каталоге, по убыванию частоты: [{name, count}]."""
    global _genres_cache
    now = datetime.now(timezone.utc).timestamp()
    if _genres_cache and _genres_cache[0] > now:
        return [dict(item) for item in _genres_cache[1]]
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT genre AS name, COUNT(*) AS count FROM film_genres GROUP BY genre"
        )
        rows = await cur.fetchall()
    # Мёржим языковые дубли одного жанра под русский канон, суммируем счётчики
    # и показываем только кураторскую витрину (см. _GENRE_SHOWCASE).
    merged: dict[str, int] = {}
    for row in rows:
        merged[_canon_genre(row["name"])] = merged.get(_canon_genre(row["name"]), 0) + row["count"]
    items = [{"name": name, "count": count} for name, count in
             sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
             if name in _GENRE_SHOWCASE]
    _genres_cache = (now + _GENRES_CACHE_TTL_SECONDS, items)
    return [dict(item) for item in items]


# ── Пара (Фаза E): приглашения, пары, совместная статистика ───────────────────
async def get_user(user_id: int) -> dict | None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, first_name, username, photo_url FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


# ── Центр уведомлений и пользовательские настройки ───────────────────────────
async def get_notification_settings(user_id: int) -> dict:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT language, telegram_enabled FROM user_notification_settings WHERE user_id = ?", (user_id,))).fetchone()
        if not row:
            return {"language": "ru", "telegram_enabled": True}
        return {"language": row["language"] if row["language"] in ("ru", "en") else "ru",
                "telegram_enabled": bool(row["telegram_enabled"])}


async def update_notification_settings(user_id: int, *, language: str | None = None,
                                       telegram_enabled: bool | None = None) -> dict:
    current = await get_notification_settings(user_id)
    next_language = language if language in ("ru", "en") else current["language"]
    next_telegram = current["telegram_enabled"] if telegram_enabled is None else bool(telegram_enabled)
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "INSERT INTO user_notification_settings (user_id, language, telegram_enabled, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET language = excluded.language, "
            "telegram_enabled = excluded.telegram_enabled, updated_at = excluded.updated_at",
            (user_id, next_language, int(next_telegram), _now()))
        await db.commit()
    return {"language": next_language, "telegram_enabled": next_telegram}


async def create_notification(*, event_type: str, recipient_id: int, actor_id: int | None,
                              entity_id: str, payload: dict, deep_link: str | None,
                              idempotency_key: str, event_id: str | None = None) -> tuple[int, bool]:
    """Persist one inbox entry exactly once; returns ``(id, created)``."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "INSERT INTO notifications (event_id, event_type, recipient_id, actor_id, entity_id, payload, deep_link, created_at, idempotency_key) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING RETURNING id",
            (event_id, event_type, recipient_id, actor_id, entity_id, json.dumps(payload, ensure_ascii=False),
             deep_link, _now(), idempotency_key))
        row = await cur.fetchone()
        if row:
            await db.commit()
            return int(row[0]), True
        row = await (await db.execute(
            "SELECT id FROM notifications WHERE idempotency_key = ?", (idempotency_key,))).fetchone()
        await db.commit()
        return int(row[0]), False


async def create_notification_delivery(notification_id: int, *, channel: str,
                                       idempotency_key: str) -> bool:
    now = _now()
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "INSERT INTO notification_deliveries (notification_id, channel, status, created_at, updated_at, idempotency_key) "
            "VALUES (?,?,'pending',?,?,?) ON CONFLICT(idempotency_key) DO NOTHING RETURNING notification_id",
            (notification_id, channel, now, now, idempotency_key))
        created = await cur.fetchone()
        await db.commit()
        return created is not None


async def finish_notification_delivery(notification_id: int, *, channel: str, sent: bool,
                                       error: str | None = None) -> None:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "UPDATE notification_deliveries SET status = ?, attempts = attempts + 1, last_error = ?, "
            "updated_at = ?, sent_at = CASE WHEN ? THEN ? ELSE sent_at END "
            "WHERE notification_id = ? AND channel = ?",
            ("sent" if sent else "failed", (error or "")[:500] if error else None,
             # PostgreSQL requires a real boolean for CASE WHEN; SQLite also
             # accepts bools, so preserving the type prevents a production-
             # only asyncpg failure after Telegram accepts a message.
             _now(), bool(sent), _now(), notification_id, channel))
        await db.commit()


async def claim_notification_delivery(notification_id: int, *, channel: str,
                                      stale_before: str | None = None) -> bool:
    """Atomically reserve a durable delivery for one worker.

    Telegram's sendMessage API does not provide an idempotency key. A process
    can therefore die after Telegram accepted a message but before we record
    the response. Such a ``sending`` row is deliberately *not* claimed again:
    duplicate bot messages are worse than missing a secondary push because the
    canonical in-app notification remains available.

    ``stale_before`` stays in the signature for compatibility with existing
    callers. Stale in-flight rows are archived separately by
    :func:`abandon_stale_notification_deliveries`.
    """
    params: list = [_now(), notification_id, channel]
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE notification_deliveries SET status = 'sending', updated_at = ? "
            "WHERE notification_id = ? AND channel = ? "
            "AND status IN ('pending', 'failed') RETURNING notification_id",
            params)
        claimed = await cur.fetchone()
        await db.commit()
        return claimed is not None


async def list_recoverable_notification_deliveries(*, stale_before: str, limit: int = 100) -> list[dict]:
    """Read only deliveries known not to have reached Telegram.

    Rows left in ``sending`` have an unknown outcome and are intentionally
    excluded. Replaying them after a deploy is how duplicate bot messages were
    created in earlier releases.
    """
    limit = max(1, min(int(limit), 500))
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT d.notification_id, d.status, d.last_error, n.recipient_id, n.payload, n.deep_link "
            "FROM notification_deliveries d JOIN notifications n ON n.id = d.notification_id "
            "WHERE d.channel = 'telegram' AND d.status IN ('pending', 'failed') "
            "ORDER BY d.updated_at ASC LIMIT ?",
            (limit,))).fetchall()
        return [dict(row) for row in rows]


async def abandon_stale_notification_deliveries(*, stale_before: str) -> int:
    """Archive uncertain in-flight Telegram sends without replaying them.

    The state only occurs if the worker was interrupted between calling
    Telegram and persisting the result. Keeping it visible for diagnostics but
    not retrying it gives the user an at-most-once bot notification guarantee.
    """
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE notification_deliveries SET status='abandoned', "
            "last_error=COALESCE(last_error, ?), updated_at=? "
            "WHERE channel='telegram' AND status='sending' AND updated_at < ?",
            ("Delivery outcome was unknown after interruption; not retried to avoid a duplicate Telegram message.",
             _now(), stale_before),
        )
        await db.commit()
        return int(cur.rowcount)


async def list_notifications(user_id: int, *, limit: int = 20, before_id: int | None = None) -> dict:
    limit = max(1, min(int(limit), 50))
    params: list = [user_id]
    where = "WHERE n.recipient_id = ?"
    if before_id is not None:
        where += " AND n.id < ?"
        params.append(int(before_id))
    params.append(limit + 1)
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT n.id, n.event_type, n.recipient_id, n.actor_id, n.entity_id, n.payload, n.deep_link, "
            "n.read_at, n.created_at, u.first_name AS actor_name, u.username AS actor_username, u.photo_url AS actor_photo_url "
            "FROM notifications n LEFT JOIN users u ON u.id = n.actor_id " + where + " ORDER BY n.id DESC LIMIT ?",
            params)).fetchall()
        unread = await (await db.execute(
            "SELECT COUNT(*) FROM notifications WHERE recipient_id = ? AND read_at IS NULL", (user_id,))).fetchone()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            item["payload"] = {}
        item["read"] = item.pop("read_at") is not None
        item["actor"] = {"id": item.pop("actor_id"), "name": item.pop("actor_name") or "",
                         "username": item.pop("actor_username"), "photo_url": item.pop("actor_photo_url")}
        items.append(item)
    return {"items": items, "unread_count": int(unread[0]),
            "next_before_id": items[-1]["id"] if has_more and items else None}


async def mark_notification_read(user_id: int, notification_id: int) -> bool:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ? AND recipient_id = ?",
            (_now(), notification_id, user_id))
        await db.commit()
        return bool(cur.rowcount)


async def mark_all_notifications_read(user_id: int) -> int:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "UPDATE notifications SET read_at = ? WHERE recipient_id = ? AND read_at IS NULL", (_now(), user_id))
        await db.commit()
        return cur.rowcount


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
            "AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at DESC LIMIT 1", (from_user, _now()))
        row = await cur.fetchone()
        return row[0] if row else None


async def get_invite_sender(token: str) -> dict | None:
    """Return only the sender of a still-actionable invite.

    The invite token is already the capability needed to accept the invitation;
    this small preview lets that same recipient see who invited them before
    deciding.  It intentionally does not expose any relationship data.
    """
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT u.id, u.first_name, u.username, u.photo_url "
            "FROM partner_invites i JOIN users u ON u.id = i.from_user "
            "WHERE i.token = ? AND i.status = 'pending' "
            "AND (i.expires_at IS NULL OR i.expires_at > ?)", (token, _now()))
        row = await cur.fetchone()
        return dict(row) if row else None


async def _lock_pair_users(db, *user_ids: int) -> None:
    """Серіалізувати зміни pair/invite для одних і тих самих користувачів.

    Інваріант «у користувача немає pending invite, якщо він уже в парі» не можна
    виразити звичайним FK між двома таблицями. Тому всі шляхи, які створюють
    пару або інвайт, беруть однаковий lock на рядки users у стабільному порядку.
    Postgres використовує row-level ``FOR UPDATE``; SQLite не має такого
    синтаксису, тож no-op UPDATE бере його єдиний writer lock.
    """
    ids = sorted({int(user_id) for user_id in user_ids})
    if not ids:
        return
    marks = ", ".join("?" for _ in ids)
    if db_runtime.uses_postgres(DATABASE_URL):
        await db.execute(
            f"SELECT id FROM users WHERE id IN ({marks}) ORDER BY id FOR UPDATE", ids)
    else:
        await db.execute(
            f"UPDATE users SET last_seen = last_seen WHERE id IN ({marks})", ids)


def _pair_id(first_user_id: int, second_user_id: int, since: str) -> str:
    low, high = sorted((int(first_user_id), int(second_user_id)))
    return f"pair:{low}:{high}:{since}"


def _pair_key(first_user_id: int, second_user_id: int) -> str:
    """Stable identity of one exact two-person pair, independent of sessions."""
    low, high = sorted((int(first_user_id), int(second_user_id)))
    return f"pair:{low}:{high}"


def _legacy_pair_session_id(pair_key: str, started_at: str) -> str:
    return f"session:{pair_key}:{started_at}"


def _parse_legacy_pair_id(pair_id: str | None) -> tuple[int, int, str] | None:
    """Read the old ``pair:<low>:<high>:<since>`` event identity safely."""
    if not pair_id or not pair_id.startswith("pair:"):
        return None
    parts = pair_id.split(":", 3)
    if len(parts) != 4:
        return None
    try:
        first_user_id, second_user_id = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if first_user_id >= second_user_id or not parts[3]:
        return None
    return first_user_id, second_user_id, parts[3]


def _new_pair_session(first_user_id: int, second_user_id: int, started_at: str) -> dict:
    first_user_id, second_user_id = sorted((int(first_user_id), int(second_user_id)))
    return {
        "id": f"session_{secrets.token_urlsafe(18)}",
        "pair_key": _pair_key(first_user_id, second_user_id),
        "first_user_id": first_user_id,
        "second_user_id": second_user_id,
        "started_at": started_at,
    }


async def _create_pair_session(db, session: dict) -> None:
    await db.execute(
        "INSERT INTO pair_sessions (id, pair_key, first_user_id, second_user_id, started_at, ended_at, created_at) "
        "VALUES (?,?,?,?,?,NULL,?)",
        (session["id"], session["pair_key"], session["first_user_id"], session["second_user_id"],
         session["started_at"], session["started_at"]))


def _new_pair_event(*, event_type: str, invitation_id: str | None = None,
                    pair_id: str | None = None, inviter_user_id: int | None = None,
                    invitee_user_id: int | None = None, actor_user_id: int | None = None) -> dict:
    return {
        "id": f"evt_{secrets.token_urlsafe(18)}", "event_type": event_type,
        "invitation_id": invitation_id, "pair_id": pair_id,
        "inviter_user_id": inviter_user_id, "invitee_user_id": invitee_user_id,
        "actor_user_id": actor_user_id, "created_at": _now(),
    }


async def _insert_pair_event(db, event: dict, *, recipients: list[tuple[int, int | None]]) -> None:
    """Write an event and its explicit delivery audience in one transaction."""
    await db.execute(
        "INSERT INTO pair_events (id, event_type, invitation_id, pair_id, inviter_user_id, invitee_user_id, actor_user_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (event["id"], event["event_type"], event["invitation_id"], event["pair_id"],
         event["inviter_user_id"], event["invitee_user_id"], event["actor_user_id"], event["created_at"]))
    for recipient_user_id, partner_user_id in recipients:
        await db.execute(
            "INSERT INTO pair_event_recipients (event_id, recipient_user_id, partner_user_id) VALUES (?,?,?) "
            "ON CONFLICT(event_id, recipient_user_id) DO NOTHING",
            (event["id"], int(recipient_user_id), int(partner_user_id) if partner_user_id is not None else None))


async def get_pair_event_recipients(event_id: str) -> list[dict]:
    """Return the immutable, transactionally stored audience of a pair event."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT recipient_user_id, partner_user_id FROM pair_event_recipients "
            "WHERE event_id = ? AND dispatched_at IS NULL ORDER BY recipient_user_id",
            (event_id,))).fetchall()
        return [{"recipient_user_id": int(row["recipient_user_id"]),
                 "partner_user_id": int(row["partner_user_id"]) if row["partner_user_id"] is not None else None}
                for row in rows]


async def mark_pair_event_recipient_dispatched(event_id: str, recipient_user_id: int) -> None:
    """A durable outbox acknowledgement after inbox/bot work has been queued."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "UPDATE pair_event_recipients SET dispatched_at = ? "
            "WHERE event_id = ? AND recipient_user_id = ? AND dispatched_at IS NULL",
            (_now(), event_id, int(recipient_user_id)))
        await db.commit()


async def list_pending_pair_events(limit: int = 100) -> list[dict]:
    """Read the durable notification outbox without scanning already dispatched events."""
    limit = max(1, min(int(limit), 500))
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT DISTINCT pe.id, pe.event_type, pe.invitation_id, pe.pair_id, pe.inviter_user_id, "
            "pe.invitee_user_id, pe.actor_user_id, pe.created_at "
            "FROM pair_events pe JOIN pair_event_recipients per ON per.event_id = pe.id "
            "WHERE per.dispatched_at IS NULL ORDER BY pe.created_at ASC LIMIT ?", (limit,))).fetchall()
        return [dict(row) for row in rows]


async def create_invite(from_user: int, *, with_state: bool = False) -> str | dict | None:
    """Створити або перевикористати єдиний pending invite.

    ``None`` означає, що користувач уже в парі. Перевірка виконується після
    взяття того самого lock, що й ``accept_invite``: інакше accept між окремими
    ``get_partner`` та INSERT міг залишити чинне запрошення у вже створеній парі.
    """
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await _lock_pair_users(db, from_user)
        cur = await db.execute("SELECT 1 FROM partners WHERE user_id = ?", (from_user,))
        if await cur.fetchone():
            await db.commit()
            return None

        cur = await db.execute(
            "SELECT token FROM partner_invites WHERE from_user = ? AND status = 'pending' "
            "AND (expires_at IS NULL OR expires_at > ?) ORDER BY created_at DESC LIMIT 1", (from_user, _now()))
        existing = await cur.fetchone()
        if existing:
            await db.commit()
            return {"token": existing[0], "created": False} if with_state else existing[0]

        token = secrets.token_urlsafe(12)
        now = _now()
        expires_at = (datetime.now(timezone.utc) + _PAIR_INVITE_TTL).isoformat()
        cur = await db.execute(
            "INSERT INTO partner_invites (token, from_user, status, created_at, expires_at) VALUES (?,?, 'pending', ?, ?) "
            "ON CONFLICT DO NOTHING RETURNING token",
            (token, from_user, now, expires_at))
        created = await cur.fetchone()
        if created:
            await db.commit()
            return {"token": created[0], "created": True} if with_state else created[0]

        # Defensive fallback for a legacy database without the partial unique
        # index or for a concurrent deployment that has not yet migrated.
        cur = await db.execute(
            "SELECT token FROM partner_invites WHERE from_user = ? AND status = 'pending' "
            "AND (expires_at IS NULL OR expires_at > ?) ORDER BY created_at DESC LIMIT 1", (from_user, _now()))
        existing = await cur.fetchone()
        await db.commit()
        if existing:
            return {"token": existing[0], "created": False} if with_state else existing[0]
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
                "SELECT from_user FROM partner_invites WHERE token = ? AND status = 'pending' "
                "AND (expires_at IS NULL OR expires_at > ?)", (token, _now()))
            row = await cur.fetchone()
            if not row:
                raise _InviteRejected("invalid")
            from_user = row["from_user"]
            if from_user == accepting_user:
                raise _InviteRejected("self")

            # Lock both sides before claiming the invite. create_invite() takes
            # the same locks, so it either completes before this transaction
            # (and is revoked below) or observes the newly created pair later.
            await _lock_pair_users(db, from_user, accepting_user)
            cur = await db.execute(
                "UPDATE partner_invites SET status='accepted', accepted_by_user_id = ? WHERE token = ? AND status = 'pending' "
                "RETURNING from_user", (accepting_user, token))
            row = await cur.fetchone()
            if not row:
                raise _InviteRejected("invalid")

            now = _now()
            session = _new_pair_session(from_user, accepting_user, now)
            await _create_pair_session(db, session)
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
            event = _new_pair_event(
                event_type="pair.invite.accepted", invitation_id=token,
                pair_id=session["id"], inviter_user_id=int(from_user),
                invitee_user_id=int(accepting_user), actor_user_id=int(accepting_user))
            await _insert_pair_event(db, event, recipients=[(int(from_user), int(accepting_user)),
                                                             (int(accepting_user), int(from_user))])
            await db.commit()
            return {"ok": True, "partner_id": from_user, "event": event}
    except _InviteRejected as e:
        return {"ok": False, "reason": e.reason}


async def register_invite_recipient(token: str, user_id: int) -> bool:
    """Remember who opened a generic share link, once and without locking it.

    Invites intentionally remain shareable capabilities.  This table is *not*
    an allow-list: it only lets cancellation/expiry reach people who actually
    saw the invitation and gives the app an honest recipient for its inbox.
    """
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "SELECT from_user FROM partner_invites WHERE token = ? AND status = 'pending' "
            "AND (expires_at IS NULL OR expires_at > ?)", (token, _now()))
        row = await cur.fetchone()
        if not row or int(row[0]) == int(user_id):
            await db.commit()
            return False
        cur = await db.execute(
            "INSERT INTO pair_invite_recipients (invite_token, user_id, created_at) VALUES (?,?,?) "
            "ON CONFLICT(invite_token, user_id) DO NOTHING RETURNING user_id",
            (token, user_id, _now()))
        created = await cur.fetchone()
        await db.commit()
        return created is not None


async def get_invite_recipients(token: str) -> list[int]:
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute(
            "SELECT user_id FROM pair_invite_recipients WHERE invite_token = ? ORDER BY user_id", (token,))
        return [int(row[0]) for row in await cur.fetchall()]


async def decline_invite(token: str, declining_user: int) -> dict:
    """Decline an invitation without deleting its audit trail."""
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT from_user FROM partner_invites WHERE token = ? AND status = 'pending' "
            "AND (expires_at IS NULL OR expires_at > ?)", (token, _now()))
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "reason": "invalid"}
        from_user = int(row["from_user"])
        if from_user == int(declining_user):
            return {"ok": False, "reason": "self"}
        cur = await db.execute(
            "UPDATE partner_invites SET status = 'declined' WHERE token = ? AND status = 'pending' RETURNING from_user",
            (token,))
        changed = await cur.fetchone()
        event = None
        if changed:
            event = _new_pair_event(event_type="pair.invite.declined", invitation_id=token,
                                     inviter_user_id=from_user, invitee_user_id=int(declining_user),
                                     actor_user_id=int(declining_user))
            await _insert_pair_event(db, event, recipients=[(int(from_user), int(declining_user))])
        await db.commit()
        return {"ok": bool(changed), "from_user": from_user, "reason": "invalid" if not changed else None,
                "event": event}


async def expire_pending_invites() -> list[dict]:
    """Atomically expire old invitations and return their known participants."""
    now = _now()
    expired: list[dict] = []
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT token, from_user FROM partner_invites WHERE status = 'pending' "
            "AND expires_at IS NOT NULL AND expires_at <= ?", (now,))
        rows = await cur.fetchall()
        for row in rows:
            cur = await db.execute(
                "UPDATE partner_invites SET status = 'expired' WHERE token = ? AND status = 'pending' RETURNING token",
                (row["token"],))
            if await cur.fetchone():
                recipients = await (await db.execute(
                    "SELECT user_id FROM pair_invite_recipients WHERE invite_token = ?", (row["token"],))).fetchall()
                event = _new_pair_event(event_type="pair.invite.expired", invitation_id=row["token"],
                                         inviter_user_id=int(row["from_user"]), actor_user_id=None)
                recipient_rows = [(int(row["from_user"]), None)]
                recipient_rows.extend((int(item[0]), int(row["from_user"])) for item in recipients)
                await _insert_pair_event(db, event, recipients=recipient_rows)
                expired.append({"token": row["token"], "from_user": int(row["from_user"]),
                                "recipient_ids": [int(item[0]) for item in recipients], "event": event})
        await db.commit()
    return expired


async def unpair(user_id: int) -> dict:
    """Разорвать только текущую симметричную пару пользователя.

    Поиск партнёра и удаление выполняются в одной транзакции. Поэтому
    запоздалый запрос не может удалить новую пару бывшего партнёра.
    """
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        cur = await db.execute("SELECT partner_id, since FROM partners WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            partner_id, since = int(row[0]), row[1]
            pair_key = _pair_key(user_id, partner_id)
            ended_at = _now()
            session = await (await db.execute(
                "SELECT id FROM pair_sessions WHERE pair_key = ? AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1", (pair_key,))).fetchone()
            if session:
                await db.execute(
                    "UPDATE pair_sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                    (ended_at, session[0]))
            origin = await (await db.execute(
                "SELECT from_user, accepted_by_user_id FROM partner_invites WHERE status = 'accepted' "
                "AND ((from_user = ? AND accepted_by_user_id = ?) OR (from_user = ? AND accepted_by_user_id = ?)) "
                "ORDER BY created_at DESC LIMIT 1", (user_id, partner_id, partner_id, user_id))).fetchone()
            await db.execute(
                "DELETE FROM partners "
                "WHERE (user_id = ? AND partner_id = ?) OR (user_id = ? AND partner_id = ?)",
                (user_id, partner_id, partner_id, user_id))
            event = _new_pair_event(
                event_type="pair.ended", pair_id=session[0] if session else _pair_id(user_id, partner_id, since),
                inviter_user_id=int(origin[0]) if origin else None,
                invitee_user_id=int(origin[1]) if origin and origin[1] is not None else None,
                actor_user_id=int(user_id))
            await _insert_pair_event(db, event, recipients=[(int(user_id), partner_id), (partner_id, int(user_id))])
            await db.commit()
            return {"kind": "ended", "partner_id": partner_id, "event": event}
        # Calling unpair while not paired also cancels the caller's pending invite.
        # We intentionally do not touch another user's newer invite or relationship.
        cur = await db.execute(
            "SELECT token FROM partner_invites WHERE from_user = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1", (user_id,))
        pending = await cur.fetchone()
        if not pending:
            await db.commit()
            return {"kind": "none"}
        token = pending[0]
        recipients = await (await db.execute(
            "SELECT user_id FROM pair_invite_recipients WHERE invite_token = ?", (token,))).fetchall()
        await db.execute(
            "UPDATE partner_invites SET status = 'cancelled' WHERE token = ? AND status = 'pending'", (token,))
        event = _new_pair_event(event_type="pair.invite.cancelled", invitation_id=token,
                                 inviter_user_id=int(user_id), actor_user_id=int(user_id))
        await _insert_pair_event(db, event, recipients=[(int(item[0]), int(user_id)) for item in recipients])
        await db.commit()
        return {"kind": "cancelled", "token": token, "recipient_ids": [int(item[0]) for item in recipients],
                "event": event}


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
    # Один INSERT ... SELECT замість get_partner() + add_to_list() у різних
    # транзакціях: фільм потрапляє тільки до партнера, який існує саме в момент
    # запису, а не до вже колишнього партнера після concurrent unpair.
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        await db.execute(
            "INSERT INTO user_films (user_id, film_id, status, added_at) "
            "SELECT partner_id, ?, 'want_to_watch', ? FROM partners WHERE user_id = ? "
            "ON CONFLICT(user_id, film_id) DO NOTHING",
            (film_id, _now(), user_id))
        await db.commit()


async def _pair_sessions_for(db, user_id: int, partner_id: int, since: str) -> list[tuple[str, str | None]]:
    """Return all saved intervals for one exact pair, with a safe legacy fallback."""
    rows = await (await db.execute(
        "SELECT started_at, ended_at FROM pair_sessions WHERE pair_key = ? ORDER BY started_at ASC",
        (_pair_key(user_id, partner_id),))).fetchall()
    sessions = [(row["started_at"], row["ended_at"]) for row in rows]
    return sessions or [(since, None)]


def _pair_session_predicate(sessions: list[tuple[str, str | None]]) -> tuple[str, list]:
    """Build portable membership SQL for pair history.

    A pair film is one where each person had meaningful activity in the same
    saved relationship interval.  ``added_at`` covers a new shared wish; the
    watch/rating dates additionally cover a film that was already in "Want"
    before the pair and was actually watched together later.  This is written
    without ``? IS NULL`` so PostgreSQL can infer every parameter type.
    """
    clauses: list[str] = []
    params: list = []
    for started_at, ended_at in sessions:
        if ended_at is None:
            a_activity = "(a.added_at >= ? OR a.watched_at >= ? OR a.rated_at >= ?)"
            b_activity = "(b.added_at >= ? OR b.watched_at >= ? OR b.rated_at >= ?)"
            clauses.append(f"({a_activity} AND {b_activity})")
            params.extend((started_at, started_at, started_at, started_at, started_at, started_at))
            continue
        a_activity = (
            "((a.added_at >= ? AND a.added_at <= ?) OR "
            "(a.watched_at >= ? AND a.watched_at <= ?) OR "
            "(a.rated_at >= ? AND a.rated_at <= ?))"
        )
        b_activity = (
            "((b.added_at >= ? AND b.added_at <= ?) OR "
            "(b.watched_at >= ? AND b.watched_at <= ?) OR "
            "(b.rated_at >= ? AND b.rated_at <= ?))"
        )
        clauses.append(f"({a_activity} AND {b_activity})")
        params.extend((
            started_at, ended_at, started_at, ended_at, started_at, ended_at,
            started_at, ended_at, started_at, ended_at, started_at, ended_at,
        ))
    return " OR ".join(clauses), params


async def pair_period_stats(user_id: int, partner_id: int, since: str) -> dict:
    """Statistics for all historical sessions of one exact pair.

    ``since`` remains as a legacy fallback for direct callers during upgrades,
    but active production pairs read every saved session for these two IDs.  A
    user's personal list is never modified or reset by this calculation.
    """
    year_now = datetime.now(timezone.utc).year
    async with db_runtime.connect(DB_PATH, DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        sessions = await _pair_sessions_for(db, user_id, partner_id, since)
        period_sql, period_params = _pair_session_predicate(sessions)
        rows = await (await db.execute(
            f"""
            SELECT f.id AS film_id, f.genres, f.actors, f.actors_photos, f.directors, f.directors_photos, f.runtime, f.title, f.poster_url,
                   a.status AS sa, a.rating AS ra, a.watched_at AS wa,
                   b.status AS sb, b.rating AS rb, b.watched_at AS wb
            FROM user_films a
            JOIN user_films b ON a.film_id = b.film_id
            JOIN films f ON f.id = a.film_id
            WHERE a.user_id = ? AND b.user_id = ? AND ({period_sql})
            """, (user_id, partner_id, *period_params))).fetchall()

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
    actor_photo_sources = _person_photo_sources(both_watched, "actors_photos")
    director_photo_sources = _person_photo_sources(both_watched, "directors_photos")
    actor_prominence = _person_credit_prominence(both_watched, "actors")
    director_prominence = _person_credit_prominence(both_watched, "directors")
    total_min = 0
    year_min, year_genre, year_actor = 0, {}, {}
    year_ratings, year_count = [], 0
    for r in both_watched:
        m = re.search(r"\d+", r["runtime"] or "")
        rt = int(m.group(0)) if m else 0
        total_min += rt
        for g in (r["genres"] or "").split(","):
            g = _canon_genre(g)
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
                g = _canon_genre(g)
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
    top_genres_pct = [
        # See get_user_stats: the count makes the percentage interpretable in
        # the pair profile while preserving the original tuple prefix.
        (g, round(c / total_refs * 100), c)
        for g, c in sorted(genre_counts.items(), key=lambda x: -x[1])[:5]
    ] if total_refs else []
    top_actors = [(n, c, *(actor_photo_sources.get(n) or [None]))
                  for n, c in _rank_people(actor_counts, actor_prominence)]
    top_directors = [(n, c, *(director_photo_sources.get(n) or [None]))
                     for n, c in _rank_people(director_counts, director_prominence)]

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
        # These rails are swipeable, not paginated.  A bounded 10/5 payload
        # keeps the profile immediate even for a couple with a large history.
        common_favorites = [{"film_id": item["film_id"], "title": item["title"], "poster_url": item["poster_url"],
                             "avg": round((item["a"] + item["b"]) / 2, 1)} for item in ranked_favorites[:10]]
        ranked_disagreements = [item for item in sorted(rated, key=lambda item: abs(item["a"] - item["b"]), reverse=True)
                                if item["a"] != item["b"]]
        disagreements = [{"film_id": item["film_id"], "title": item["title"], "poster_url": item["poster_url"],
                          "a": item["a"], "b": item["b"], "diff": abs(item["a"] - item["b"])}
                         for item in ranked_disagreements[:5]]
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
