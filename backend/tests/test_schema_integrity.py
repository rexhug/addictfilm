import sqlite3
import tempfile
import unittest
from pathlib import Path

import aiosqlite
import database as db
import db_runtime
import migrations as schema_migrations


class FreshSchemaIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def test_fresh_schema_contains_role_and_catalog_cache_columns(self):
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            tables = await (await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
            users = await (await conn.execute("PRAGMA table_info(users)")).fetchall()
            films = await (await conn.execute("PRAGMA table_info(films)")).fetchall()
            user_films = await (await conn.execute("PRAGMA table_info(user_films)")).fetchall()
            user_films_fks = await (await conn.execute("PRAGMA foreign_key_list(user_films)")).fetchall()
            migrations = await (await conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )).fetchall()

        self.assertTrue({"role", "acquisition_source", "acquisition_param"}.issubset(
            {row[1] for row in users}))
        self.assertTrue({"movie_enrichment_jobs", "movie_recommendation_profiles",
                         "movie_recommendation_profile_overrides",
                         "worker_heartbeats", "wishlist_random_state",
                         "wishlist_random_picks", "review_identities",
                         "review_reports", "film_identity_locks"}.issubset(
            {row[0] for row in tables}))
        self.assertTrue({"kp_id", "search_text", "poster_checked_at", "artwork_checked_at", "actor_photos_checked_at",
                         "directors_photos", "director_photos_checked_at",
                         "media_type", "media_type_source",
                         "hero_url", "hero_type", "hero_source", "hero_quality_score",
                         "hero_width", "hero_height", "hero_updated_at",
                         "hero_checked_at", "hero_fit", "hero_focus_x", "hero_focus_y",
                         "poster_display_state", "poster_display_url",
                         "poster_reject_reason", "movie_flow_state",
                         "movie_flow_reason", "cast_json", "cast_checked_at"}.issubset(
            {row[1] for row in films}))
        self.assertEqual({row[2] for row in user_films_fks}, {"users", "films"})
        self.assertTrue({"commented_at", "comment_status"}.issubset(
            {row[1] for row in user_films}))
        self.assertEqual([row[0] for row in migrations], [
            schema_migrations._SCHEMA_MIGRATION_DIRECTOR_PHOTOS,
            schema_migrations._SCHEMA_MIGRATION_LEGACY_COLUMNS,
            schema_migrations._SCHEMA_MIGRATION_PERSON_PORTRAIT_COMPLETENESS,
            schema_migrations._SCHEMA_MIGRATION_PERSON_PORTRAIT_RETRY,
            schema_migrations._SCHEMA_MIGRATION_PAIR_HISTORY,
            schema_migrations._SCHEMA_MIGRATION_PAIR_NOTIFICATION_EVENTS,
            schema_migrations._SCHEMA_MIGRATION_PAIR_NOTIFICATION_OUTBOX,
            schema_migrations._SCHEMA_MIGRATION_PAIR_NOTIFICATIONS,
            schema_migrations._SCHEMA_MIGRATION_RECOMMENDATIONS,
            schema_migrations._SCHEMA_MIGRATION_RECOMMENDATION_RESULTS,
            schema_migrations._SCHEMA_MIGRATION_SEARCH_TEXT_DIRECTORS,
            schema_migrations._SCHEMA_MIGRATION_EDITORIAL_COLLECTIONS,
            schema_migrations._SCHEMA_MIGRATION_FEATURED_COLLECTIONS,
            schema_migrations._SCHEMA_MIGRATION_MEDIA_TYPE,
            schema_migrations._SCHEMA_MIGRATION_MOVIE_ENRICHMENT,
            schema_migrations._SCHEMA_MIGRATION_SESSION_VERSIONS,
            schema_migrations._SCHEMA_MIGRATION_WISHLIST_ROULETTE,
            schema_migrations._SCHEMA_MIGRATION_WORKER_HEARTBEATS,
            schema_migrations._SCHEMA_MIGRATION_HERO_MEDIA,
            schema_migrations._SCHEMA_MIGRATION_HERO_PRESENTATION,
            schema_migrations._SCHEMA_MIGRATION_MOVIE_FLOW_MODERATION,
            schema_migrations._SCHEMA_MIGRATION_CANONICAL_CAST,
            schema_migrations._SCHEMA_MIGRATION_FILM_IDENTITY,
            schema_migrations._SCHEMA_MIGRATION_PUBLIC_REVIEWS,
            schema_migrations._SCHEMA_MIGRATION_USER_ACQUISITION,
            schema_migrations._SCHEMA_MIGRATION_HERO_POLICY_V3,
        ])

    async def test_foreign_keys_reject_orphaned_user_film_rows(self):
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            with self.assertRaises(aiosqlite.IntegrityError) as error:
                await conn.execute(
                    "INSERT INTO user_films (user_id, film_id, status) VALUES (?,?,?)",
                    (999, 999, "want_to_watch"),
                )

        self.assertIn("FOREIGN KEY", str(error.exception).upper())

    async def test_migration_adds_role_and_preserves_latest_pending_invite(self):
        # Simulate a database created before role support and before the pending
        # invite uniqueness invariant existed.
        self.temp_dir.cleanup()
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.temp_dir.name) / "legacy.db")
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.executescript("""
                CREATE TABLE users (
                    id BIGINT PRIMARY KEY,
                    first_name TEXT,
                    username TEXT,
                    created_at TEXT,
                    last_seen TEXT
                );
                CREATE TABLE partner_invites (
                    token TEXT PRIMARY KEY,
                    from_user BIGINT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE films (
                    id INTEGER PRIMARY KEY,
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
                    created_at TEXT
                );
                INSERT INTO users VALUES (1, 'One', NULL, '2026-01-01', '2026-01-01');
                INSERT INTO partner_invites VALUES ('old', 1, 'pending', '2026-01-01T00:00:00+00:00');
                INSERT INTO partner_invites VALUES ('new', 1, 'pending', '2026-01-02T00:00:00+00:00');
                INSERT INTO films (id, imdb_id, title, title_original, actors)
                VALUES (1, 'kp_301', 'Матрица', 'The Matrix', 'Киану Ривз');
            """)
            await conn.commit()

        await db.init_db()

        self.assertIsNone(await db.get_user_role(1))
        self.assertEqual(await db.get_pending_invite(1), "new")
        self.assertEqual(await db.get_film_id_by_source("k", "301"), 1)
        self.assertEqual((await db.search_catalog("the matrix"))[0]["ref"], "301")
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            cur = await conn.execute("SELECT status FROM partner_invites WHERE token = 'old'")
            self.assertEqual((await cur.fetchone())[0], "superseded")


class AlreadyExistsDetectionTests(unittest.TestCase):
    """A migration must distinguish "already applied" from "actually broken".

    The old check read the driver's message text, so a PostgreSQL server with a
    non-English locale — or a reworded asyncpg string — would turn a harmless
    re-run into a failed boot.
    """

    def test_detects_postgres_duplicate_column_by_sqlstate(self):
        exc = Exception("будь-який текст будь-якою мовою")
        exc.sqlstate = "42701"
        self.assertTrue(schema_migrations._is_already_exists_error(exc))

    def test_detects_sqlite_duplicate_column_by_message(self):
        self.assertTrue(schema_migrations._is_already_exists_error(
            sqlite3.OperationalError("duplicate column name: hero_url")))

    def test_does_not_swallow_permission_error(self):
        exc = Exception("permission denied for table films")
        exc.sqlstate = "42501"
        self.assertFalse(schema_migrations._is_already_exists_error(exc))
