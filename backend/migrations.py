"""Ordered, idempotent schema upgrades and the journal that records them.

Thirty-odd functions that exist for one caller: run_schema_migrations().  They
lived beside the query layer only because that is where they were written; no
repository has ever called one.  Keeping them here means a new migration is
added without opening the file that holds the queries.
"""
import logging
import sqlite3

import aiosqlite
import db_session
from database import _enqueue_enrichment, _set_film_genres
from db_helpers import (
    _catalog_search_text,
    _legacy_pair_session_id,
    _now,
    _pair_key,
    _parse_legacy_pair_id,
    _split_genres,
    _split_people,
)

logger = logging.getLogger(__name__)


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
_SCHEMA_MIGRATION_EDITORIAL_COLLECTIONS = "2026-07-27-editorial-collections-v1"
_SCHEMA_MIGRATION_FEATURED_COLLECTIONS = "2026-07-27-featured-collections-v1"
_SCHEMA_MIGRATION_MEDIA_TYPE = "2026-07-27-media-type-v1"
_SCHEMA_MIGRATION_MOVIE_ENRICHMENT = "2026-07-27-movie-enrichment-v1"
_SCHEMA_MIGRATION_WORKER_HEARTBEATS = "2026-07-27-worker-heartbeats-v1"
_SCHEMA_MIGRATION_WISHLIST_ROULETTE = "2026-07-27-wishlist-roulette-v1"
_SCHEMA_MIGRATION_SESSION_VERSIONS = "2026-07-27-session-versions-v1"
_SCHEMA_MIGRATION_HERO_MEDIA = "2026-07-28-hero-media-v1"
_SCHEMA_MIGRATION_HERO_PRESENTATION = "2026-07-28-hero-presentation-v1"
_SCHEMA_MIGRATION_MOVIE_FLOW_MODERATION = "2026-07-28-movie-flow-moderation-v1"
_SCHEMA_MIGRATION_PUBLIC_REVIEWS = "2026-07-29-public-reviews-v1"
_SCHEMA_MIGRATION_FILM_IDENTITY = "2026-07-29-film-identity-v1"
_SCHEMA_MIGRATION_CANONICAL_CAST = "2026-07-29-canonical-cast-v1"
_SCHEMA_MIGRATION_USER_ACQUISITION = "2026-08-01-user-acquisition-v1"


# PostgreSQL SQLSTATE codes for "the object I am adding is already there".
# Matching on message text used to break on a driver or locale change; the
# code is part of the wire protocol and does not.
_ALREADY_EXISTS_SQLSTATES = frozenset({
    "42701",   # duplicate_column
    "42P07",   # duplicate_table
    "42710",   # duplicate_object
})


def _is_already_exists_error(exc: BaseException) -> bool:
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if sqlstate in _ALREADY_EXISTS_SQLSTATES:
        return True
    # SQLite has no SQLSTATE.  Its message set is small, stable and English-only
    # because the library ships its own strings, so text matching is safe here
    # and only here.
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        return "duplicate column" in message or "already exists" in message
    return False


async def _add_column_if_missing(table: str, col_def: str) -> None:
    """ALTER TABLE ADD COLUMN, idempotently, each attempt in its OWN
    connection/transaction: under PostgreSQL any error (e.g. the column already
    exists) aborts the WHOLE transaction — inside a shared init_db() block it
    would drag down every following CREATE TABLE IF NOT EXISTS.
    """
    try:
        async with db_session.connect() as db:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            await db.commit()
    except Exception as exc:
        # Syntax, permission, disk and connectivity errors must NOT be hidden:
        # they used to leave an instance on a partially upgraded schema silently.
        if _is_already_exists_error(exc):
            return
        logger.exception("Schema migration failed while adding %s.%s", table, col_def)
        raise


async def _schema_migration_applied(version: str) -> bool:
    async with db_session.connect() as db:
        row = await (await db.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
        )).fetchone()
        return row is not None


async def _record_schema_migration(version: str) -> None:
    async with db_session.connect() as db:
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
    async with db_session.connect() as db:
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
    async with db_session.connect() as db:
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
    async with db_session.connect() as db:
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
    notification_id = "SERIAL PRIMARY KEY" if db_session.uses_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    async with db_session.connect() as db:
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
    async with db_session.connect() as db:
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
    async with db_session.connect() as db:
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
    history_id = "SERIAL PRIMARY KEY" if db_session.uses_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    async with db_session.connect() as db:
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


async def _apply_editorial_collections_migration() -> None:
    """Редакционная модель поверх СУЩЕСТВУЮЩИХ таблиц collections/collection_films.

    Намеренно не заводим параллельные editorial_* таблицы: id подборок уже живут
    в проде (и в публичных ссылках), а дублирующая модель означала бы две правды
    и рискованный перенос. Добавляем недостающие поля и бэкфилим так, чтобы
    ВИДИМОСТЬ КОНТЕНТА НЕ ИЗМЕНИЛАСЬ: всё, что сейчас публично, остаётся
    published (снять с публикации админ сможет уже руками из интерфейса).
    """
    for col in (
        "status TEXT NOT NULL DEFAULT 'published'",   # draft | published | archived
        "description TEXT",
        "cover_url TEXT",
        "sort_order INTEGER NOT NULL DEFAULT 0",
        "updated_by BIGINT",
        "updated_at TEXT",
        "published_by BIGINT",
        "published_at TEXT",
        "version INTEGER NOT NULL DEFAULT 1",
    ):
        await _add_column_if_missing("collections", col)
    for col in ("position INTEGER NOT NULL DEFAULT 0", "added_by BIGINT"):
        await _add_column_if_missing("collection_films", col)

    async with db_session.connect() as db:
        # Бэкфил метаданных: существующий контент считается опубликованным.
        await db.execute(
            "UPDATE collections SET updated_at = created_at WHERE updated_at IS NULL")
        await db.execute(
            "UPDATE collections SET updated_by = created_by WHERE updated_by IS NULL")
        await db.execute(
            "UPDATE collections SET published_at = created_at, published_by = created_by "
            "WHERE status = 'published' AND published_at IS NULL")
        await db.execute(
            "UPDATE collection_films SET added_by = ("
            "  SELECT created_by FROM collections c WHERE c.id = collection_films.collection_id"
            ") WHERE added_by IS NULL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id          {id_col},
                actor_id    BIGINT NOT NULL,
                actor_role  TEXT NOT NULL,
                action      TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id   TEXT NOT NULL,
                details     TEXT,
                created_at  TEXT NOT NULL
            )
        """.format(id_col="BIGSERIAL PRIMARY KEY" if db_session.uses_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"))
        await db.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_entity "
                         "ON admin_audit_log(entity_type, entity_id, created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_actor "
                         "ON admin_audit_log(actor_id, created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_collections_public "
                         "ON collections(status, sort_order)")
        await db.commit()

    # Порядок внутри подборки: до миграции он был неявным (added_at ASC) —
    # сохраняем ровно его, иначе у существующих подборок «переедут» фильмы.
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT collection_id, film_id FROM collection_films "
            "WHERE position = 0 ORDER BY collection_id, added_at ASC, film_id ASC")
        rows = [dict(r) for r in await cur.fetchall()]
    positions: dict[int, int] = {}
    for row in rows:
        collection_id = row["collection_id"]
        positions[collection_id] = positions.get(collection_id, 0) + 1
        async with db_session.connect() as db:
            await db.execute(
                "UPDATE collection_films SET position = ? WHERE collection_id = ? AND film_id = ?",
                (positions[collection_id], collection_id, row["film_id"]))
            await db.commit()


async def _apply_featured_collections_migration() -> None:
    """Формат отображения подборки: обычная карточка или крупный блок на главной.

    display_type — ТОЛЬКО про представление: он не влияет ни на права, ни на
    публикацию (черновик остаётся приватным в любом формате). Существующие
    подборки получают 'standard', то есть внешний вид продакшена не меняется.
    backdrop_url отдельный: вертикальная обложка в ландшафтном блоке смотрится
    плохо, поэтому переиспользовать cover_url вслепую нельзя.
    """
    await _add_column_if_missing("collections", "display_type TEXT NOT NULL DEFAULT 'standard'")
    await _add_column_if_missing("collections", "backdrop_url TEXT")
    async with db_session.connect() as db:
        # Значение по умолчанию покрывает существующие строки, но добитие делает
        # миграцию идемпотентной и чинит возможный NULL от ранних попыток.
        await db.execute("UPDATE collections SET display_type = 'standard' WHERE display_type IS NULL")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_collections_display "
                         "ON collections(display_type, status, sort_order)")
        await db.commit()


async def _apply_media_type_migration() -> None:
    """Тип записи каталога отдельной колонкой: фильм, сериал, эпизод, короткометражка.

    Раньше признак приходил от провайдера и выбрасывался, из-за чего в подбор
    ФИЛЬМОВ попадали сериалы (22-минутный мультсериал предлагался как фильм).
    Колонка заполняется провайдером при сохранении и разово добивается скриптом
    scripts/backfill_media_type.py; NULL означает «нет данных», а не «фильм».
    """
    await _add_column_if_missing("films", "media_type TEXT")
    await _add_column_if_missing("films", "media_type_source TEXT")


async def _apply_movie_enrichment_migration() -> None:
    """Долговечная очередь обогащения и версионированные профили признаков.

    Очередь живёт в БД, а не в BackgroundTasks: веб-процесс может перезапуститься
    сразу после ответа, и незавершённое обогащение просто исчезло бы. Состояние
    в таблице переживает и рестарт, и падение воркера.

    Типы намеренно портируемые (TEXT под ISO-время и JSON), как во всей схеме
    проекта: одна и та же миграция обязана примениться и к SQLite локально, и к
    Postgres в облаке.
    """
    job_id = "BIGSERIAL PRIMARY KEY" if db_session.uses_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    async with db_session.connect() as db:
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS movie_enrichment_jobs (
                id             {job_id},
                film_id        INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
                job_type       TEXT NOT NULL DEFAULT 'full_enrichment'
                               CHECK (job_type IN ('full_enrichment','metadata_refresh','feature_rebuild')),
                status         TEXT NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending','processing','retry','completed','failed','dead_letter')),
                priority       INTEGER NOT NULL DEFAULT 0,
                requested_feature_version  TEXT NOT NULL,
                requested_taxonomy_version TEXT NOT NULL,
                expected_source_hash TEXT,
                attempts       INTEGER NOT NULL DEFAULT 0,
                max_attempts   INTEGER NOT NULL DEFAULT 6,
                run_after      TEXT NOT NULL,
                locked_at      TEXT,
                locked_by      TEXT,
                last_error_code    TEXT,
                last_error_message TEXT,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL,
                completed_at   TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_enrichment_jobs_claim "
                         "ON movie_enrichment_jobs(status, run_after, priority DESC, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_enrichment_jobs_film "
                         "ON movie_enrichment_jobs(film_id)")
        # Частичный уникальный индекс — единственная защита от дублей заданий,
        # которая работает при нескольких воркерах одновременно.
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_enrichment_active_job "
            "ON movie_enrichment_jobs(film_id, job_type, requested_feature_version, "
            "requested_taxonomy_version) WHERE status IN ('pending','processing','retry')")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS movie_recommendation_profiles (
                film_id        INTEGER PRIMARY KEY REFERENCES films(id) ON DELETE CASCADE,
                status         TEXT NOT NULL
                               CHECK (status IN ('pending','ready','low_confidence','failed','stale')),
                content_type   TEXT NOT NULL
                               CHECK (content_type IN ('movie','tv','episode','short','unknown')),
                feature_version   TEXT NOT NULL,
                taxonomy_version  TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                semantic_model_version TEXT,
                source_hash    TEXT NOT NULL,
                genres         TEXT NOT NULL DEFAULT '[]',
                themes         TEXT NOT NULL DEFAULT '[]',
                tones          TEXT NOT NULL DEFAULT '[]',
                audience_flags TEXT NOT NULL DEFAULT '[]',
                energy REAL NOT NULL CHECK (energy BETWEEN 0 AND 1),
                pace   REAL NOT NULL CHECK (pace BETWEEN 0 AND 1),
                tension REAL NOT NULL CHECK (tension BETWEEN 0 AND 1),
                darkness REAL NOT NULL CHECK (darkness BETWEEN 0 AND 1),
                humor REAL NOT NULL CHECK (humor BETWEEN 0 AND 1),
                emotionality REAL NOT NULL CHECK (emotionality BETWEEN 0 AND 1),
                complexity REAL NOT NULL CHECK (complexity BETWEEN 0 AND 1),
                realism REAL NOT NULL CHECK (realism BETWEEN 0 AND 1),
                peak_darkness REAL NOT NULL DEFAULT 0.5 CHECK (peak_darkness BETWEEN 0 AND 1),
                confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                validation_level TEXT NOT NULL DEFAULT 'valid',
                warnings       TEXT NOT NULL DEFAULT '[]',
                evidence       TEXT NOT NULL DEFAULT '{}',
                calculated_at  TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_profiles_status "
                         "ON movie_recommendation_profiles(status, content_type)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_profiles_versions "
                         "ON movie_recommendation_profiles(feature_version, taxonomy_version)")
        # Ручная правка живёт отдельно и переживает любой пересчёт: иначе
        # исправление, сделанное руками, стирается следующим же обогащением.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS movie_recommendation_profile_overrides (
                film_id       INTEGER PRIMARY KEY REFERENCES films(id) ON DELETE CASCADE,
                override_data TEXT NOT NULL,
                reason        TEXT NOT NULL,
                created_by    BIGINT NOT NULL,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
        """)
        await db.commit()


async def _apply_worker_heartbeats_migration() -> None:
    """Пульс фоновых процессов отдельной миграцией.

    Урок: таблицу нельзя дописать в уже применённую миграцию — журнал её
    пропустит, и в проде объекта просто не будет. Новый объект = новая версия.
    """
    async with db_session.connect() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS worker_heartbeats (
                name       TEXT PRIMARY KEY,
                worker_id  TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.commit()


async def _apply_wishlist_roulette_migration() -> None:
    """Состояние рулетки по списку «Хочу посмотреть».

    Прежний выбор был равномерным, но без памяти: один и тот же фильм мог
    выпадать подряд, а человек воспринимает это как «оно сломалось». Храним
    номер цикла и показанные в нём фильмы — тогда каждый фильм показывается
    один раз за круг, а история показов не удаляется ради перезапуска круга.
    """
    async with db_session.connect() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wishlist_random_state (
                user_id      BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                cycle_number INTEGER NOT NULL DEFAULT 1,
                updated_at   TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wishlist_random_picks (
                user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                film_id      INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
                cycle_number INTEGER NOT NULL,
                shown_at     TEXT NOT NULL,
                PRIMARY KEY (user_id, film_id, cycle_number)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_wishlist_picks_cycle "
                         "ON wishlist_random_picks(user_id, cycle_number)")
        await db.commit()


async def _apply_session_versions_migration() -> None:
    """Версии движка, политики, таксономии и признаков в самой сессии.

    Без них сессия, начатая до деплоя, доигрывается уже другой семантикой:
    ответы даны по старой анкете, а замена карточки считается новой. Колонки
    NULL-совместимые — существующие сессии остаются валидными и просто помечены
    как «версия неизвестна», то есть доигрываются прежней логикой.
    """
    for column in ("engine_version TEXT", "policy_version TEXT",
                   "taxonomy_version TEXT", "feature_version TEXT"):
        await _add_column_if_missing("recommendation_sessions", column)


async def _apply_hero_media_migration() -> None:
    """Выбранное сервером изображение для полноэкранного экрана подбора.

    Почему отдельные колонки, а не переиспользование backdrop_url: наличие
    backdrop_url не доказывает ПРИГОДНОСТЬ. Там встречаются и вертикальные
    обложки, и 640×360. hero_* хранит именно результат проверки — что выбрано,
    откуда, с каким счётом и какого размера.

    CHECK намеренно не навешиваем: колонки добавляются в существующую большую
    таблицу через ALTER, и добавление ограничения на живой Postgres — это
    отдельная блокирующая операция, ради которой не стоит рисковать стартовой
    миграцией. Допустимые значения проверяет hero_media.HERO_TYPES/HERO_SOURCES
    на записи, и это закрыто тестом.

    Все колонки NULL-совместимы: каталог до бекфила обязан работать как раньше.
    """
    for col_def in (
        "hero_url TEXT",
        "hero_type TEXT",
        "hero_source TEXT",
        "hero_quality_score REAL",
        "hero_width INTEGER",
        "hero_height INTEGER",
        "hero_updated_at TEXT",
        # Отдельно от hero_updated_at: фильм без IMDb-id и без постера не даёт
        # выбора вовсе, и без отметки о ПОПЫТКЕ он возвращался бы в кандидаты
        # на каждом круге воркера — то есть внешний обход каталога на каждом
        # деплое, ровно то, чего быть не должно.
        "hero_checked_at TEXT",
    ):
        await _add_column_if_missing("films", col_def)
    async with db_session.connect() as db:
        await db.execute("CREATE INDEX IF NOT EXISTS idx_films_hero_checked "
                         "ON films(hero_checked_at)")
        await db.commit()


async def _apply_hero_presentation_migration() -> None:
    """Persistent art direction and exact-file poster moderation."""
    for col_def in (
        "hero_fit TEXT",
        "hero_focus_x REAL",
        "hero_focus_y REAL",
        "poster_display_state TEXT",
        "poster_display_url TEXT",
        "poster_reject_reason TEXT",
    ):
        await _add_column_if_missing("films", col_def)


async def _apply_movie_flow_moderation_migration() -> None:
    """Durable curator override for recommendation eligibility."""
    await _add_column_if_missing("films", "movie_flow_state TEXT")
    await _add_column_if_missing("films", "movie_flow_reason TEXT")


async def _apply_public_reviews_migration() -> None:
    """Add public-review metadata without exposing historical private notes.

    ``user_films`` remains the source of truth for the current rating/comment.
    The small identity table supplies a stable numeric cursor and lets reports
    survive edits without copying review text into a second content table.
    """
    await _add_column_if_missing("user_films", "commented_at TEXT")
    await _add_column_if_missing("user_films", "comment_status TEXT")
    review_id = "SERIAL PRIMARY KEY" if db_session.uses_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    report_id = "SERIAL PRIMARY KEY" if db_session.uses_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    async with db_session.connect() as db:
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS review_identities (
                id         {review_id},
                user_id    BIGINT NOT NULL,
                film_id    INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (user_id, film_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (film_id) REFERENCES films(id)
            )
        """)
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS review_reports (
                id          {report_id},
                review_id   INTEGER NOT NULL,
                reporter_id BIGINT NOT NULL,
                reason      TEXT,
                created_at  TEXT NOT NULL,
                UNIQUE (review_id, reporter_id),
                FOREIGN KEY (review_id) REFERENCES review_identities(id),
                FOREIGN KEY (reporter_id) REFERENCES users(id)
            )
        """)
        # Historical comments were written when the product promised a private
        # note. They must never become public merely because a deploy ran.
        await db.execute(
            "UPDATE user_films SET comment_status = 'private_legacy' "
            "WHERE comment IS NOT NULL AND TRIM(comment) <> '' AND comment_status IS NULL"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_films_public_reviews "
            "ON user_films(film_id, comment_status, commented_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_reports_review "
            "ON review_reports(review_id)"
        )
        await db.commit()


async def _apply_canonical_cast_migration() -> None:
    """Additive canonical cast storage; legacy fields remain the rollback path."""
    await _add_column_if_missing("films", "cast_json TEXT")
    await _add_column_if_missing("films", "cast_checked_at TEXT")


async def _apply_user_acquisition_migration() -> None:
    """Откуда пришёл пользователь. Обе колонки необязательные и заполняются
    только у новых регистраций: у зарегистрированных РАНЬШЕ они останутся NULL,
    и это честный ответ «неизвестно», а не повод дописать им «direct»."""
    await _add_column_if_missing("users", "acquisition_source TEXT")
    await _add_column_if_missing("users", "acquisition_param TEXT")


def _row_time(row: dict, *fields: str) -> str:
    """Comparable ISO timestamp for conflict resolution during a film merge."""
    return max((str(row.get(field) or "") for field in fields), default="")


def _merge_user_film_values(current: dict, duplicate: dict) -> dict:
    """Merge two states of one user's same logical movie without losing history.

    A later explicit rating/comment wins.  Passive list additions never turn a
    watched movie back into ``want_to_watch`` and the oldest ``added_at`` is
    retained as the beginning of the user's personal history.
    """
    rows = (current, duplicate)
    rated = [row for row in rows if row.get("rating") is not None]
    rating_row = max(
        rated,
        key=lambda row: _row_time(row, "rated_at", "watched_at", "added_at"),
        default=current,
    )
    commented = [row for row in rows if (row.get("comment") or "").strip()]
    comment_row = max(
        commented,
        key=lambda row: _row_time(row, "commented_at", "rated_at", "added_at"),
        default=None,
    )
    added_values = [str(row.get("added_at")) for row in rows if row.get("added_at")]
    watched_values = [str(row.get("watched_at")) for row in rows if row.get("watched_at")]
    rated_values = [str(row.get("rated_at")) for row in rows if row.get("rated_at")]
    return {
        "status": "watched" if any(row.get("status") == "watched" for row in rows)
        else "want_to_watch",
        "rating": rating_row.get("rating") if rated else None,
        "comment": comment_row.get("comment") if comment_row else None,
        "comment_status": comment_row.get("comment_status") if comment_row else None,
        "added_at": min(added_values) if added_values else None,
        "watched_at": max(watched_values) if watched_values else None,
        "rated_at": max(rated_values) if rated_values else None,
        "commented_at": (
            _row_time(comment_row, "commented_at") or None
            if comment_row else None
        ),
    }


async def _merge_film_duplicate(db, canonical_row: dict, duplicate_row: dict) -> None:
    """Move every durable reference to ``canonical_row`` and delete a duplicate.

    This function is intentionally conservative: callers only pass rows with
    the exact same title, year and non-empty poster where one id is a synthetic
    ``kp_*`` alias and the other is a real IMDb id.
    """
    canonical_id = int(canonical_row["id"])
    duplicate_id = int(duplicate_row["id"])

    # One current state per user/movie.  When both aliases were used, merge the
    # two rows according to their real activity timestamps.
    cur = await db.execute("SELECT * FROM user_films WHERE film_id = ?", (duplicate_id,))
    for duplicate_state_row in await cur.fetchall():
        duplicate_state = dict(duplicate_state_row)
        user_id = duplicate_state["user_id"]
        existing_cur = await db.execute(
            "SELECT * FROM user_films WHERE user_id = ? AND film_id = ?",
            (user_id, canonical_id),
        )
        existing_row = await existing_cur.fetchone()
        if existing_row is None:
            await db.execute(
                "UPDATE user_films SET film_id = ? WHERE user_id = ? AND film_id = ?",
                (canonical_id, user_id, duplicate_id),
            )
            continue
        merged = _merge_user_film_values(dict(existing_row), duplicate_state)
        await db.execute(
            "UPDATE user_films SET status = ?, rating = ?, comment = ?, added_at = ?, "
            "watched_at = ?, rated_at = ?, commented_at = ?, comment_status = ? "
            "WHERE user_id = ? AND film_id = ?",
            (
                merged["status"], merged["rating"], merged["comment"], merged["added_at"],
                merged["watched_at"], merged["rated_at"], merged["commented_at"],
                merged["comment_status"], user_id, canonical_id,
            ),
        )
        await db.execute(
            "DELETE FROM user_films WHERE user_id = ? AND film_id = ?",
            (user_id, duplicate_id),
        )

    # Stable public-review identities and abuse reports must follow the merged
    # movie.  Duplicate reports by the same reporter collapse idempotently.
    cur = await db.execute(
        "SELECT * FROM review_identities WHERE film_id = ?", (duplicate_id,)
    )
    for identity_row in await cur.fetchall():
        identity = dict(identity_row)
        existing_cur = await db.execute(
            "SELECT id FROM review_identities WHERE user_id = ? AND film_id = ?",
            (identity["user_id"], canonical_id),
        )
        existing = await existing_cur.fetchone()
        if existing is None:
            await db.execute(
                "UPDATE review_identities SET film_id = ? WHERE id = ?",
                (canonical_id, identity["id"]),
            )
            continue
        await db.execute(
            "INSERT INTO review_reports (review_id, reporter_id, reason, created_at) "
            "SELECT ?, reporter_id, reason, created_at FROM review_reports WHERE review_id = ? "
            "ON CONFLICT(review_id, reporter_id) DO NOTHING",
            (existing["id"], identity["id"]),
        )
        await db.execute("DELETE FROM review_reports WHERE review_id = ?", (identity["id"],))
        await db.execute("DELETE FROM review_identities WHERE id = ?", (identity["id"],))

    # Editorial collections keep their order and authorship.  A pre-existing
    # canonical entry wins; otherwise the duplicate entry is simply re-keyed.
    cur = await db.execute(
        "SELECT * FROM collection_films WHERE film_id = ?", (duplicate_id,)
    )
    for collection_row in await cur.fetchall():
        item = dict(collection_row)
        existing_cur = await db.execute(
            "SELECT 1 FROM collection_films WHERE collection_id = ? AND film_id = ?",
            (item["collection_id"], canonical_id),
        )
        if await existing_cur.fetchone():
            await db.execute(
                "DELETE FROM collection_films WHERE collection_id = ? AND film_id = ?",
                (item["collection_id"], duplicate_id),
            )
        else:
            await db.execute(
                "UPDATE collection_films SET film_id = ? "
                "WHERE collection_id = ? AND film_id = ?",
                (canonical_id, item["collection_id"], duplicate_id),
            )

    # Set-like projections are copied with their natural uniqueness keys.
    await db.execute(
        "INSERT INTO film_genres (film_id, genre) "
        "SELECT ?, genre FROM film_genres WHERE film_id = ? "
        "ON CONFLICT(film_id, genre) DO NOTHING",
        (canonical_id, duplicate_id),
    )
    await db.execute("DELETE FROM film_genres WHERE film_id = ?", (duplicate_id,))
    await db.execute(
        "INSERT INTO film_recommendation_tags "
        "(film_id, tag, confidence, source, version, updated_at) "
        "SELECT ?, tag, confidence, source, version, updated_at "
        "FROM film_recommendation_tags WHERE film_id = ? "
        "ON CONFLICT(film_id, tag, source, version) DO NOTHING",
        (canonical_id, duplicate_id),
    )
    await db.execute(
        "DELETE FROM film_recommendation_tags WHERE film_id = ?", (duplicate_id,)
    )

    # Historical recommendation events are user history, so preserve them.
    await db.execute(
        "UPDATE recommendation_history SET film_id = ? WHERE film_id = ?",
        (canonical_id, duplicate_id),
    )
    cur = await db.execute(
        "SELECT * FROM wishlist_random_picks WHERE film_id = ?", (duplicate_id,)
    )
    for pick_row in await cur.fetchall():
        pick = dict(pick_row)
        await db.execute(
            "INSERT INTO wishlist_random_picks "
            "(user_id, film_id, cycle_number, shown_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, film_id, cycle_number) DO NOTHING",
            (pick["user_id"], canonical_id, pick["cycle_number"], pick["shown_at"]),
        )
    await db.execute(
        "DELETE FROM wishlist_random_picks WHERE film_id = ?", (duplicate_id,)
    )

    # Overrides are human input.  Preserve a duplicate override if the
    # canonical movie has none; derived profiles/jobs can be safely rebuilt.
    override_cur = await db.execute(
        "SELECT 1 FROM movie_recommendation_profile_overrides WHERE film_id = ?",
        (canonical_id,),
    )
    if await override_cur.fetchone():
        await db.execute(
            "DELETE FROM movie_recommendation_profile_overrides WHERE film_id = ?",
            (duplicate_id,),
        )
    else:
        await db.execute(
            "UPDATE movie_recommendation_profile_overrides SET film_id = ? WHERE film_id = ?",
            (canonical_id, duplicate_id),
        )
    await db.execute("DELETE FROM movie_enrichment_jobs WHERE film_id = ?", (duplicate_id,))
    await db.execute(
        "DELETE FROM movie_recommendation_profiles WHERE film_id = ?", (duplicate_id,)
    )

    # Delete the synthetic row before moving its kp_id; the existing partial
    # unique index correctly forbids both rows from owning the same kp id.
    await db.execute("DELETE FROM films WHERE id = ?", (duplicate_id,))

    merged_genres = ", ".join(dict.fromkeys(
        _split_genres(canonical_row.get("genres"))
        + _split_genres(duplicate_row.get("genres"))
    )) or None
    merged_directors = ", ".join(dict.fromkeys(
        _split_people(canonical_row.get("directors"))
        + _split_people(duplicate_row.get("directors"))
    )) or None
    merged_actors = ", ".join(dict.fromkeys(
        _split_people(canonical_row.get("actors"))
        + _split_people(duplicate_row.get("actors"))
    )) or None
    merged_kp_id = canonical_row.get("kp_id") or duplicate_row.get("kp_id")
    merged_search_text = _catalog_search_text(
        canonical_row.get("title") or duplicate_row.get("title"),
        canonical_row.get("title_original") or duplicate_row.get("title_original"),
        merged_actors, merged_directors, canonical_row.get("imdb_id"), merged_kp_id,
    )
    await db.execute(
        "UPDATE films SET kp_id = ?, genres = ?, directors = ?, actors = ?, search_text = ?, "
        "title_original = COALESCE(NULLIF(title_original, ''), ?), "
        "runtime = COALESCE(NULLIF(runtime, ''), ?), "
        "imdb_rating = COALESCE(NULLIF(imdb_rating, ''), ?), "
        "kp_rating = COALESCE(NULLIF(kp_rating, ''), ?), "
        "imdb_votes = COALESCE(NULLIF(imdb_votes, ''), ?), "
        "plot = COALESCE(NULLIF(plot, ''), ?), "
        "poster_url = COALESCE(NULLIF(poster_url, ''), ?), "
        "backdrop_url = COALESCE(NULLIF(backdrop_url, ''), ?), "
        "age_rating = COALESCE(NULLIF(age_rating, ''), ?), "
        "actors_photos = COALESCE(NULLIF(actors_photos, ''), ?), "
        "directors_photos = COALESCE(NULLIF(directors_photos, ''), ?), "
        "media_type = COALESCE(NULLIF(media_type, ''), ?), "
        "media_type_source = COALESCE(NULLIF(media_type_source, ''), ?) "
        "WHERE id = ?",
        (
            merged_kp_id, merged_genres, merged_directors, merged_actors,
            merged_search_text, duplicate_row.get("title_original"),
            duplicate_row.get("runtime"), duplicate_row.get("imdb_rating"),
            duplicate_row.get("kp_rating"), duplicate_row.get("imdb_votes"),
            duplicate_row.get("plot"), duplicate_row.get("poster_url"),
            duplicate_row.get("backdrop_url"), duplicate_row.get("age_rating"),
            duplicate_row.get("actors_photos"), duplicate_row.get("directors_photos"),
            duplicate_row.get("media_type"), duplicate_row.get("media_type_source"),
            canonical_id,
        ),
    )
    await _set_film_genres(db, canonical_id, merged_genres)
    await _enqueue_enrichment(db, canonical_id, priority=20)
    logger.warning(
        "catalog dedupe: merged film_id=%s (%s) into film_id=%s (%s)",
        duplicate_id, duplicate_row.get("imdb_id"),
        canonical_id, canonical_row.get("imdb_id"),
    )


async def _apply_film_identity_migration() -> None:
    """Repair high-confidence IMDb/Kinopoisk aliases and prevent new races."""
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        # A tiny durable lock registry serializes concurrent catalog inserts
        # across all Fly machines.  One row per logical catalog identity is a
        # bounded, auditable cost and works identically in SQLite/Postgres.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS film_identity_locks (
                identity_key TEXT PRIMARY KEY,
                created_at   TEXT NOT NULL
            )
        """)
        groups_cur = await db.execute(
            "SELECT title, COALESCE(year, '') AS year_key, poster_url "
            "FROM films WHERE poster_url IS NOT NULL AND TRIM(poster_url) <> '' "
            "GROUP BY title, COALESCE(year, ''), poster_url HAVING COUNT(*) > 1"
        )
        for group_row in await groups_cur.fetchall():
            group = dict(group_row)
            films_cur = await db.execute(
                "SELECT * FROM films WHERE title = ? AND COALESCE(year, '') = ? "
                "AND poster_url = ? ORDER BY id",
                (group["title"], group["year_key"], group["poster_url"]),
            )
            records = [dict(row) for row in await films_cur.fetchall()]
            real_imdb = [
                row for row in records
                if str(row.get("imdb_id") or "").lower().startswith("tt")
            ]
            synthetic = [
                row for row in records
                if str(row.get("imdb_id") or "").lower().startswith("kp_")
            ]
            # Exact poster/title/year plus exactly one real IMDb record is
            # intentionally strict; ambiguous remakes are left for an operator.
            if len(real_imdb) != 1 or not synthetic:
                continue
            canonical = real_imdb[0]
            for duplicate in synthetic:
                await _merge_film_duplicate(db, canonical, duplicate)
                canonical["kp_id"] = canonical.get("kp_id") or duplicate.get("kp_id")
                canonical["genres"] = ", ".join(dict.fromkeys(
                    _split_genres(canonical.get("genres"))
                    + _split_genres(duplicate.get("genres"))
                )) or None
                canonical["directors"] = ", ".join(dict.fromkeys(
                    _split_people(canonical.get("directors"))
                    + _split_people(duplicate.get("directors"))
                )) or None
                canonical["actors"] = ", ".join(dict.fromkeys(
                    _split_people(canonical.get("actors"))
                    + _split_people(duplicate.get("actors"))
                )) or None
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_films_kp_id "
            "ON films(kp_id) WHERE kp_id IS NOT NULL"
        )
        await db.commit()


async def run_schema_migrations() -> None:
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
        (_SCHEMA_MIGRATION_EDITORIAL_COLLECTIONS, _apply_editorial_collections_migration),
        (_SCHEMA_MIGRATION_FEATURED_COLLECTIONS, _apply_featured_collections_migration),
        (_SCHEMA_MIGRATION_MEDIA_TYPE, _apply_media_type_migration),
        (_SCHEMA_MIGRATION_MOVIE_ENRICHMENT, _apply_movie_enrichment_migration),
        (_SCHEMA_MIGRATION_WORKER_HEARTBEATS, _apply_worker_heartbeats_migration),
        (_SCHEMA_MIGRATION_WISHLIST_ROULETTE, _apply_wishlist_roulette_migration),
        (_SCHEMA_MIGRATION_SESSION_VERSIONS, _apply_session_versions_migration),
        (_SCHEMA_MIGRATION_HERO_MEDIA, _apply_hero_media_migration),
        (_SCHEMA_MIGRATION_HERO_PRESENTATION, _apply_hero_presentation_migration),
        (_SCHEMA_MIGRATION_MOVIE_FLOW_MODERATION, _apply_movie_flow_moderation_migration),
        (_SCHEMA_MIGRATION_PUBLIC_REVIEWS, _apply_public_reviews_migration),
        (_SCHEMA_MIGRATION_FILM_IDENTITY, _apply_film_identity_migration),
        (_SCHEMA_MIGRATION_CANONICAL_CAST, _apply_canonical_cast_migration),
        (_SCHEMA_MIGRATION_USER_ACQUISITION, _apply_user_acquisition_migration),
    )
    for version, migration in migrations:
        if await _schema_migration_applied(version):
            continue
        await migration()
        await _record_schema_migration(version)
