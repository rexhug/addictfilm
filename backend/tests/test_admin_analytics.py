"""Приватная сводка по аудитории: доступ, атрибуция и границы суток.

Три обещания. Цифры видит только администратор. Источник регистрации ставится
один раз и не переписывается. «Сегодня» — календарные сутки UTC, а не последние
24 часа, и пользователи, зарегистрированные до появления атрибуции, честно
считаются неизвестными, а не прямыми заходами.
"""
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import database as db
import db_runtime
import deps
import main
import routers.admin as admin_router_module
from auth import extract_start_param
from fastapi import HTTPException


class AcquisitionNormalizationTests(unittest.TestCase):
    def test_absent_parameter_is_a_direct_visit(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(db.normalize_acquisition(value), ("direct", None))

    def test_an_invite_token_is_bucketed_but_never_stored(self):
        source, param = db.normalize_acquisition("inv_9f3c1d2e5a7b")
        self.assertEqual(source, "pair_invite")
        # Токен приглашения — действующий секрет, в аналитике ему не место.
        self.assertIsNone(param)

    def test_a_shared_film_link_keeps_only_the_channel(self):
        self.assertEqual(db.normalize_acquisition("film_26"), ("shared_film", None))

    def test_a_campaign_label_is_kept_verbatim(self):
        self.assertEqual(db.normalize_acquisition("promo-june_2026"),
                         ("campaign", "promo-june_2026"))

    def test_an_impossible_value_is_counted_but_not_stored(self):
        """Telegram такого выдать не может: считаем каналом, значение не храним."""
        for value in ("a" * 65, "drop table users", "промо", "x;y"):
            with self.subTest(value=value):
                source, param = db.normalize_acquisition(value)
                self.assertEqual(source, "campaign")
                self.assertIsNone(param)

    def test_start_param_is_read_from_the_signed_string(self):
        self.assertEqual(
            extract_start_param("auth_date=1&start_param=promo-1&hash=abc"), "promo-1")
        self.assertEqual(extract_start_param("auth_date=1&hash=abc"), "")
        self.assertEqual(extract_start_param(""), "")


class AdminAnalyticsAccessTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, role):
        with patch.object(deps, "_effective_role", return_value=role):
            return await main.require_admin_user(user={"id": 42})

    async def test_only_the_admin_role_passes(self):
        self.assertEqual((await self._call("admin"))["id"], 42)

    async def test_an_editor_cannot_read_audience_numbers(self):
        """Редактор ведёт подборки; размер аудитории к этой работе не относится."""
        with self.assertRaises(HTTPException) as ctx:
            await self._call("editor")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_an_ordinary_user_is_denied(self):
        for role in (None, "", "user", "viewer"):
            with self.subTest(role=role), self.assertRaises(HTTPException) as ctx:
                await self._call(role)
            self.assertEqual(ctx.exception.status_code, 403)

    async def test_a_client_supplied_role_is_ignored(self):
        with patch.object(deps, "_effective_role", return_value=None), \
                self.assertRaises(HTTPException) as ctx:
            await main.require_admin_user(user={"id": 42, "role": "admin"})
        self.assertEqual(ctx.exception.status_code, 403)

    def test_the_endpoint_is_wired_to_the_admin_gate(self):
        """Гейт объявлен на маршруте, а не проверяется внутри обработчика."""
        # The endpoint moved into the admin router; app.routes no longer
        # flattens included routers in this FastAPI version, so the router is
        # where the declaration now lives. The assertion below is unchanged.
        route = next(r for r in admin_router_module.router.routes
                     if getattr(r, "path", "") == "/api/admin/analytics")
        gates = {d.call for d in route.dependant.dependencies}
        self.assertIn(main.require_admin_user, gates)
        self.assertNotIn(main.require_editor, gates)


class AdminAnalyticsDataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old = (db.DB_PATH, db.DATABASE_URL, db._PG)
        db.DB_PATH = str(Path(self.temp_dir.name) / "analytics.db")
        db.DATABASE_URL, db._PG = "", False
        await db.init_db()

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old
        self.temp_dir.cleanup()

    async def _register(self, user_id, start_param=None, *, created_at=None, last_seen=None):
        await db.upsert_user({"id": user_id, "first_name": f"U{user_id}"},
                             start_param=start_param)
        if created_at or last_seen:
            async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
                if created_at:
                    await conn.execute("UPDATE users SET created_at = ? WHERE id = ?",
                                       (created_at.isoformat(), user_id))
                if last_seen:
                    await conn.execute("UPDATE users SET last_seen = ? WHERE id = ?",
                                       (last_seen.isoformat(), user_id))
                await conn.commit()

    async def _source_of(self, user_id):
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            conn.row_factory = db.aiosqlite.Row
            row = await (await conn.execute(
                "SELECT acquisition_source, acquisition_param FROM users WHERE id = ?",
                (user_id,))).fetchone()
        return dict(row)

    async def test_the_first_touch_wins_and_is_never_rewritten(self):
        await self._register(1, "promo-june")
        # Тот же человек возвращается по ссылке «Поделиться» и по приглашению.
        await db.upsert_user({"id": 1, "first_name": "U1"}, start_param="film_26")
        await db.upsert_user({"id": 1, "first_name": "U1"}, start_param="inv_token")
        await db.upsert_user({"id": 1, "first_name": "U1"})
        stored = await self._source_of(1)
        self.assertEqual(stored["acquisition_source"], "campaign")
        self.assertEqual(stored["acquisition_param"], "promo-june")

    async def test_an_invite_token_never_reaches_the_database(self):
        await self._register(2, "inv_9f3c1d2e5a7b")
        stored = await self._source_of(2)
        self.assertEqual(stored["acquisition_source"], "pair_invite")
        self.assertIsNone(stored["acquisition_param"])

    async def test_today_is_a_calendar_utc_day_not_the_last_24_hours(self):
        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        await self._register(1, created_at=now - timedelta(hours=2))   # сегодня
        await self._register(2, created_at=now - timedelta(hours=14))  # вчера, но <24ч
        await self._register(3, created_at=now - timedelta(days=9))
        await self._register(4, created_at=now - timedelta(days=40))
        report = await db.user_analytics(now=now)
        self.assertEqual(report["total_users"], 4)
        self.assertEqual(report["new_users"]["today"], 1)
        self.assertEqual(report["new_users"]["7d"], 2)
        self.assertEqual(report["new_users"]["30d"], 3)
        self.assertEqual(report["timezone"], "UTC")

    async def test_active_users_count_recent_visits_not_registrations(self):
        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        await self._register(1, created_at=now - timedelta(days=200),
                             last_seen=now - timedelta(days=1))
        await self._register(2, created_at=now - timedelta(days=200),
                             last_seen=now - timedelta(days=30))
        report = await db.user_analytics(now=now)
        self.assertEqual(report["total_users"], 2)
        self.assertEqual(report["new_users"]["30d"], 0)
        self.assertEqual(report["active_users_7d"], 1)

    async def test_users_from_before_attribution_are_unknown_not_direct(self):
        await self._register(1, "promo-june")
        await self._register(2)
        async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
            await conn.execute("UPDATE users SET acquisition_source = NULL WHERE id = 2")
            await conn.commit()
        buckets = {row["source"]: row["users"] for row in
                   (await db.user_analytics())["acquisition"]}
        self.assertEqual(buckets, {"campaign": 1, "unknown": 1})
        self.assertNotIn("direct", buckets)

    async def test_the_report_carries_no_personal_data(self):
        await db.upsert_user(
            {"id": 777_000_111, "first_name": "Денис", "username": "rare_handle",
             "photo_url": "https://t.me/i/userpic/rare.jpg"},
            start_param="promo-june")
        payload = repr(await db.user_analytics())
        for leaked in ("Денис", "rare_handle", "userpic", "777000111", "777_000_111"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, payload)

    async def test_an_empty_installation_reports_zeroes_instead_of_failing(self):
        report = await db.user_analytics()
        self.assertEqual(report["total_users"], 0)
        self.assertEqual(report["active_users_7d"], 0)
        self.assertEqual(report["new_users"], {"today": 0, "7d": 0, "30d": 0})
        self.assertEqual(report["acquisition"], [])


if __name__ == "__main__":
    unittest.main()
