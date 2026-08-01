import asyncio
import importlib
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import cast
import cast as cast_model
import database as db
import kinopoisk
import wikidata
from enrichment import cast_backfill
from enrichment.cast_backfill import _run, enrich_film_cast


def kp_person(
    person_id, name, *, en_name=None, photo=None, role=None, profession="actor",
):
    return {
        "id": person_id, "name": name, "enName": en_name,
        "photo": photo, "description": role, "enProfession": profession,
    }


class CanonicalCastTests(unittest.TestCase):
    def test_kinopoisk_extracts_twelve_in_provider_order_with_identity_and_roles(self):
        people = [
            kp_person(i, f"Актёр {i}", en_name=f"Actor {i}", role=f"Role {i}")
            for i in range(1, 15)
        ]
        result = kinopoisk.extract_cast(people)
        self.assertEqual(len(result), 12)
        self.assertEqual([m["person_id"] for m in result[:3]], ["1", "2", "3"])
        self.assertEqual(result[0]["original_name"], "Actor 1")
        self.assertEqual(result[0]["character"], "Role 1")
        self.assertEqual(result[0]["billing_order"], 0)

    def test_non_actors_and_duplicate_person_ids_are_removed(self):
        result = kinopoisk.extract_cast([
            kp_person(1, "Lead"),
            kp_person(99, "Director", profession="director"),
            kp_person(1, "Duplicate"),
            kp_person(2, "Second"),
        ])
        self.assertEqual([m["name"] for m in result], ["Lead", "Second"])

    def test_cast_documents_resolve_omdb_rows_by_imdb_in_one_batch(self):
        payload = {
            "docs": [
                {
                    "id": 101,
                    "externalId": {"imdb": "tt1172049"},
                    "persons": [kp_person(1, "Jake Gyllenhaal")],
                },
                {
                    "id": 102,
                    "externalId": {"imdb": "tt0137523"},
                    "persons": [kp_person(2, "Brad Pitt")],
                },
            ],
        }
        with (
            patch.object(kinopoisk, "KINOPOISK_TOKENS", ["test"]),
            patch("kinopoisk._request", AsyncMock(return_value=payload)) as request,
        ):
            result = asyncio.run(kinopoisk.cast_documents_by_imdb([
                "tt1172049", "tt0137523", "tt1172049",
            ]))
        self.assertEqual(set(result), {"tt1172049", "tt0137523"})
        self.assertEqual(result["tt1172049"]["id"], 101)
        self.assertEqual(request.await_count, 1)

    def test_missing_photo_never_reorders_a_lead(self):
        result = kinopoisk.extract_cast([
            kp_person(1, "Lead"),
            kp_person(2, "Supporting", photo="https://example.test/support.jpg"),
        ])
        self.assertEqual([m["name"] for m in result], ["Lead", "Supporting"])

    def test_merge_adds_only_portraits_and_preserves_canonical_facts(self):
        canonical = kinopoisk.extract_cast([
            kp_person(1, "Разрушение", en_name="Jake Gyllenhaal", role="Davis"),
            kp_person(2, "Наоми Уоттс", en_name="Naomi Watts", role="Karen"),
        ])
        extra = [
            {
                "wikidata_id": "Q1", "name": "Naomi Watts", "billing_order": 0,
                "source": "wikidata", "photo_url": "https://commons.test/naomi.jpg",
            },
            {
                "wikidata_id": "Q2", "name": "Jake Gyllenhaal", "billing_order": 1,
                "source": "wikidata", "photo_url": "https://commons.test/jake.jpg",
            },
            {
                "wikidata_id": "Q3", "name": "Unrelated", "billing_order": 2,
                "source": "wikidata", "photo_url": "https://commons.test/no.jpg",
            },
        ]
        merged = cast.merge_portrait_fallbacks(canonical, extra)
        self.assertEqual([m["name"] for m in merged], ["Разрушение", "Наоми Уоттс"])
        self.assertEqual([m["character"] for m in merged], ["Davis", "Karen"])
        self.assertEqual([m["source"] for m in merged], ["kinopoisk", "kinopoisk"])
        self.assertEqual(merged[0]["fallback_photo_urls"], [])
        self.assertEqual(merged[0]["photo_url"], "https://commons.test/jake.jpg")

    def test_ambiguous_names_are_not_merged(self):
        canonical = [{"name": "Alex", "billing_order": 0, "source": "kinopoisk"}]
        extras = [
            {"wikidata_id": "Q1", "name": "Alex", "billing_order": 0,
             "photo_url": "https://x.test/1.jpg"},
            {"wikidata_id": "Q2", "name": "Alex", "billing_order": 1,
             "photo_url": "https://x.test/2.jpg"},
        ]
        self.assertIsNone(cast.merge_portrait_fallbacks(canonical, extras)[0]["photo_url"])

    def test_broken_json_is_empty(self):
        self.assertEqual(cast.decode_cast("{not-json"), [])

    def test_legacy_payload_keeps_order_and_fallbacks(self):
        canonical = kinopoisk.extract_cast([
            kp_person(1, "Lead", photo="https://x.test/lead.jpg"),
            kp_person(2, "Second"),
        ])
        canonical[0]["fallback_photo_urls"] = ["https://x.test/fallback.jpg"]
        legacy = cast.legacy_actor_photos(canonical)
        self.assertEqual([m["name"] for m in legacy], ["Lead", "Second"])
        self.assertEqual(legacy[0]["fallback_photo_urls"], ["https://x.test/fallback.jpg"])

    def test_http_portrait_verdicts_keep_temporary_failures_retryable(self):
        self.assertEqual(cast.portrait_http_verdict(404), "rejected")
        self.assertEqual(cast.portrait_http_verdict(410), "rejected")
        self.assertEqual(cast.portrait_http_verdict(429), "unknown")
        self.assertEqual(cast.portrait_http_verdict(503), "unknown")

    def test_verified_fallback_is_promoted_without_reordering_actor(self):
        member = {
            "name": "Lead", "photo_url": "https://x.test/bad.jpg",
            "fallback_photo_urls": ["https://x.test/good.jpg"],
        }
        updated = cast.apply_portrait_probes(member, {
            "https://x.test/bad.jpg": cast.PortraitProbe("rejected"),
            "https://x.test/good.jpg": cast.PortraitProbe("ok", 330, 440, 20_000),
        })
        self.assertEqual(updated["photo_url"], "https://x.test/good.jpg")
        self.assertEqual(updated["photo_state"], cast.PHOTO_VERIFIED)

    def test_unknown_is_not_permanently_rejected(self):
        member = {"name": "Lead", "photo_url": "https://x.test/slow.jpg"}
        updated = cast.apply_portrait_probes(member, {
            "https://x.test/slow.jpg": cast.PortraitProbe("unknown"),
        })
        self.assertEqual(updated["photo_url"], "https://x.test/slow.jpg")
        self.assertEqual(updated["photo_state"], cast.PHOTO_UNKNOWN)

    def test_all_rejected_is_clean_missing_image(self):
        member = {"name": "Lead", "photo_url": "https://x.test/bad.jpg"}
        updated = cast.apply_portrait_probes(member, {
            "https://x.test/bad.jpg": cast.PortraitProbe("rejected"),
        })
        self.assertIsNone(updated["photo_url"])
        self.assertEqual(updated["fallback_photo_urls"], [])
        self.assertEqual(updated["photo_state"], cast.PHOTO_REJECTED)


class CanonicalCastDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path, self.old_url, self.old_pg = db.DB_PATH, db.DATABASE_URL, db._PG
        db.DB_PATH = str(Path(self.temp_dir.name) / "cast.db")
        db.DATABASE_URL = ""
        db._PG = False
        await db.init_db()

    async def asyncTearDown(self):
        db.DB_PATH, db.DATABASE_URL, db._PG = self.old_path, self.old_url, self.old_pg
        self.temp_dir.cleanup()

    async def test_atomic_update_keeps_legacy_and_canonical_order_equal(self):
        film_id = await db.get_or_create_film("tt0012345", "Test")
        canonical = kinopoisk.extract_cast([
            kp_person(10, "Lead"), kp_person(20, "Second"),
        ])
        self.assertTrue(await db.update_film_cast(film_id, canonical))
        film = await db.get_film(film_id)
        self.assertEqual(film["actors"], "Lead, Second")
        self.assertEqual(
            [item["name"] for item in json.loads(film["actors_photos"])],
            ["Lead", "Second"],
        )
        self.assertEqual(
            [item["name"] for item in db.decode_film_cast(film["cast_json"])],
            ["Lead", "Second"],
        )
        self.assertEqual(film["cast_checked_at"], film["actor_photos_checked_at"])

    async def test_cast_update_never_replaces_an_existing_kinopoisk_identity(self):
        film_id = await db.get_or_create_film(
            "tt0012346", "Identity", kp_id="original-kp",
        )
        canonical = kinopoisk.extract_cast([kp_person(10, "Lead")])
        self.assertTrue(
            await db.update_film_cast(film_id, canonical, kp_id="resolved-kp")
        )
        film = await db.get_film(film_id)
        self.assertEqual(film["kp_id"], "original-kp")

    async def test_person_detail_is_bounded_and_only_for_unresolved_leading_people(self):
        film_id = await db.get_or_create_film("tt0099999", "Demolition", kp_id="99")
        film = await db.get_film(film_id)
        document = {
            "persons": [
                kp_person(i, f"Actor {i}", photo=None) for i in range(1, 7)
            ]
        }
        get_person = AsyncMock(side_effect=lambda person_id: {
            "id": person_id, "photo": f"https://x.test/{person_id}.jpg",
        })
        with (
            patch("enrichment.cast_backfill.kinopoisk.get_movie", AsyncMock(return_value=document)),
            patch("enrichment.cast_backfill.kinopoisk.get_person", get_person),
            patch("enrichment.cast_backfill.wikidata.get_cast_by_imdb", AsyncMock(return_value={})),
        ):
            outcome = await enrich_film_cast(
                film, dry_run=True, person_detail_limit=3,
            )
        self.assertEqual(outcome.person_detail_requests, 3)
        self.assertEqual(get_person.await_count, 3)
        self.assertEqual(
            [m["photo_url"] for m in outcome.canonical[:4]],
            [
                "https://x.test/1.jpg", "https://x.test/2.jpg",
                "https://x.test/3.jpg", None,
            ],
        )

    async def test_missing_kp_id_resolves_kinopoisk_cast_by_imdb(self):
        film_id = await db.get_or_create_film("tt1172049", "Разрушение")
        film = await db.get_film(film_id)
        document = {
            "id": 842493,
            "persons": [
                kp_person(1, "Джейк Джилленхол", photo="https://x.test/jake.jpg"),
                kp_person(2, "Наоми Уоттс", photo="https://x.test/naomi.jpg"),
            ],
        }
        with (
            patch(
                "enrichment.cast_backfill.kinopoisk.cast_documents_by_imdb",
                AsyncMock(return_value={"tt1172049": document}),
            ) as resolve,
            patch(
                "enrichment.cast_backfill.wikidata.get_cast_by_imdb",
                AsyncMock(return_value={}),
            ),
        ):
            outcome = await enrich_film_cast(film)
        self.assertEqual(outcome.kp_id, "842493")
        self.assertEqual(outcome.kinopoisk_cast_count, 2)
        self.assertEqual(
            [member["name"] for member in outcome.canonical],
            ["Джейк Джилленхол", "Наоми Уоттс"],
        )
        stored = await db.get_film(film_id)
        self.assertEqual(stored["kp_id"], "842493")
        resolve.assert_awaited_once_with(["tt1172049"])

    async def test_dry_run_does_not_write_or_advance_timestamp(self):
        film_id = await db.get_or_create_film("tt0088888", "Dry", kp_id="88")
        film = await db.get_film(film_id)
        document = {"persons": [kp_person(1, "Lead", photo="https://x.test/a.jpg")]}
        with (
            patch("enrichment.cast_backfill.kinopoisk.get_movie", AsyncMock(return_value=document)),
            patch("enrichment.cast_backfill.wikidata.get_cast_by_imdb", AsyncMock(return_value={})),
        ):
            await enrich_film_cast(film, dry_run=True)
        after = await db.get_film(film_id)
        self.assertIsNone(after["cast_json"])
        self.assertIsNone(after["cast_checked_at"])

    async def test_cli_closes_provider_sessions_when_backfill_fails(self):
        args = SimpleNamespace(
            film_id=None,
            limit=20,
            batch_size=5,
            concurrency=2,
            validate_portraits=False,
            person_detail_limit_per_film=3,
            dry_run=True,
        )
        kinopoisk_close = AsyncMock()
        wikidata_close = AsyncMock()
        with (
            patch(
                "enrichment.cast_backfill.db.films_for_cast_backfill",
                AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
            patch("enrichment.cast_backfill.kinopoisk.aclose", kinopoisk_close),
            patch("enrichment.cast_backfill.wikidata.aclose", wikidata_close),
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await _run(args)
        kinopoisk_close.assert_awaited_once_with()
        wikidata_close.assert_awaited_once_with()

class WikidataPrefetchScopeTests(unittest.IsolatedAsyncioTestCase):
    """Кому именно уходят пакетные запросы.

    Документ Kinopoisk нужен только строкам без kp_id. Wikidata — каждому фильму
    с валидным IMDb-идентификатором: наличие kp_id ничего не говорит о том, есть
    ли у актёра свободная фотография на Commons. Раньше оба запроса шли по одному
    списку, и фильмы с kp_id оставались вообще без запасных портретов.
    """

    def _args(self, **overrides):
        base = dict(film_id=None, limit=20, batch_size=5, concurrency=2,
                    validate_portraits=False, person_detail_limit_per_film=3,
                    dry_run=True)
        base.update(overrides)
        return SimpleNamespace(**base)

    async def _run_with(self, films, wikidata_cast, *, document=None):
        # Записываем ВСЕ вызовы: у cast_documents_by_imdb есть ещё и подушный
        # запасной путь внутри enrich_film_cast, и смешивать его с пакетным
        # префетчем нельзя — проверяем именно первый, пакетный.
        seen = {"wikidata": [], "legacy": [], "passed": []}

        async def _wikidata(imdb_ids, max_actors=10):
            seen["wikidata"].append(list(imdb_ids))
            return {imdb_id: list(wikidata_cast) for imdb_id in imdb_ids}

        async def _documents(imdb_ids):
            seen["legacy"].append(list(imdb_ids))
            return {}

        real_enrich = cast_backfill.enrich_film_cast

        async def _spy(film, **kwargs):
            seen["passed"].append((film["id"], list(kwargs.get("wikidata_cast") or [])))
            return await real_enrich(film, **kwargs)

        with (
            patch("enrichment.cast_backfill.db.films_for_cast_backfill",
                  AsyncMock(return_value=films)),
            patch("enrichment.cast_backfill.wikidata.get_cast_by_imdb", _wikidata),
            patch("enrichment.cast_backfill.kinopoisk.cast_documents_by_imdb", _documents),
            patch("enrichment.cast_backfill.kinopoisk.get_movie",
                  AsyncMock(return_value=document)),
            patch("enrichment.cast_backfill.enrich_film_cast", _spy),
            patch("enrichment.cast_backfill.kinopoisk.aclose", AsyncMock()),
            patch("enrichment.cast_backfill.wikidata.aclose", AsyncMock()),
        ):
            await _run(self._args())
        return seen

    async def test_a_film_with_a_kp_id_still_gets_wikidata_portraits(self):
        film = {"id": 26, "imdb_id": "tt1172049", "kp_id": "842493",
                "title": "Разрушение",
                "cast_json": json.dumps([
                    {"person_id": "22692", "name": "Джейк Джилленхол",
                     "billing_order": 0, "source": "kinopoisk",
                     "photo_url": "https://st.kp.yandex.net/22692.jpg"},
                    {"person_id": "27221", "name": "Наоми Уоттс",
                     "billing_order": 1, "source": "kinopoisk",
                     "photo_url": "https://st.kp.yandex.net/27221.jpg"},
                ], ensure_ascii=False)}
        researched = [
            # Wikidata отдаёт СВОЙ порядок — здесь Уоттс первая.
            {"name": "Наоми Уоттс", "photo_url": "https://commons.wikimedia.org/w/naomi.jpg"},
            {"name": "Джейк Джилленхол", "photo_url": "https://commons.wikimedia.org/w/jake.jpg"},
        ]
        # Фильм с kp_id разрешается через kp_id, а не через IMDb-мост.
        seen = await self._run_with([film], researched,
                                    document={"id": "842493", "persons": []})

        self.assertEqual(seen["wikidata"][0], ["tt1172049"], "Wikidata должна получить этот фильм")
        self.assertEqual(seen["legacy"], [], "у фильма есть kp_id — legacy-запрос не нужен")
        self.assertEqual(len(seen["passed"][0][1]), 2, "в обогащение ушёл пустой состав")

    async def test_a_mixed_batch_splits_the_two_lookups(self):
        films = [
            {"id": 26, "imdb_id": "tt1172049", "kp_id": "842493", "title": "С kp_id",
             "cast_json": None},
            {"id": 23, "imdb_id": "tt0116996", "kp_id": None, "title": "Без kp_id",
             "cast_json": None},
        ]
        seen = await self._run_with(films, [], document={"id": "842493", "persons": []})
        self.assertEqual(sorted(seen["wikidata"][0]), ["tt0116996", "tt1172049"])
        self.assertEqual(seen["legacy"][0], ["tt0116996"],
                         "пакетный legacy-запрос обязан получить только фильм без kp_id")

    async def test_an_empty_wikidata_answer_stays_harmless(self):
        film = {"id": 26, "imdb_id": "tt1172049", "kp_id": "842493", "title": "Кино",
                "cast_json": json.dumps([
                    {"person_id": "1", "name": "Главный", "billing_order": 0,
                     "source": "kinopoisk", "photo_url": None}], ensure_ascii=False)}
        seen = await self._run_with([film], [])
        self.assertEqual(seen["passed"][0][1], [])


class WikidataFallbackMergeTests(unittest.TestCase):
    """Портрет Commons становится кандидатом, а порядок остаётся кинопоисковым."""

    def test_a_commons_portrait_joins_without_touching_the_order(self):
        canonical = cast_model.normalize_cast([
            {"person_id": "22692", "name": "Джейк Джилленхол", "billing_order": 0,
             "source": "kinopoisk", "photo_url": "https://st.kp.yandex.net/22692.jpg"},
            {"person_id": "27221", "name": "Наоми Уоттс", "billing_order": 1,
             "source": "kinopoisk", "photo_url": "https://st.kp.yandex.net/27221.jpg"},
        ])
        researched = [
            {"name": "Наоми Уоттс", "photo_url": "https://commons.wikimedia.org/w/naomi.jpg"},
            {"name": "Джейк Джилленхол", "photo_url": "https://commons.wikimedia.org/w/jake.jpg"},
        ]
        merged = cast_model.merge_portrait_fallbacks(canonical, researched)

        self.assertEqual([person["name"] for person in merged],
                         ["Джейк Джилленхол", "Наоми Уоттс"])
        self.assertEqual([person["billing_order"] for person in merged], [0, 1])
        candidates = [person["photo_url"], *person["fallback_photo_urls"]] \
            if (person := merged[0]) else []
        self.assertIn("https://commons.wikimedia.org/w/jake.jpg", candidates)


class PortraitProbeSafetyTests(unittest.TestCase):
    """Проверка портрета не имеет права УДАЛЯТЬ данные без доказательства.

    Единственное доказательство того, что картинки нет, — ответ сервера 404 или
    410. Всё остальное (запрет доступа, throttling, отказ сервера, сеть) — это
    неспособность проверить. Разница практическая: Wikimedia отвечает 403 на
    запрос без контактного User-Agent, и трактовка «403 = битый файл» стирала
    живые портреты у актёров.
    """

    KP = "https://st.kp.yandex.net/primary.jpg"
    COMMONS = "https://commons.wikimedia.org/wiki/Special:FilePath/portrait.jpg"

    def _probes(self, **verdicts) -> dict:
        return {
            url: (
                cast.PortraitProbe("ok", 330, 440, 20_000)
                if verdict == "ok" else cast.PortraitProbe(verdict)
            )
            for url, verdict in verdicts.items()
        }

    def test_access_failures_do_not_destroy_portrait_candidates(self):
        self.assertEqual(cast.portrait_http_verdict(401), "unknown")
        self.assertEqual(cast.portrait_http_verdict(403), "unknown")
        self.assertEqual(cast.portrait_http_verdict(404), "rejected")
        self.assertEqual(cast.portrait_http_verdict(410), "rejected")

    def test_forbidden_probe_keeps_commons_candidate_retryable(self):
        updated = cast.apply_portrait_probes(
            {"name": "Lead", "photo_url": self.COMMONS, "fallback_photo_urls": []},
            {self.COMMONS: cast.PortraitProbe(cast.portrait_http_verdict(403))},
        )
        self.assertEqual(updated["photo_url"], self.COMMONS)
        self.assertEqual(updated["photo_state"], cast.PHOTO_UNKNOWN)

    def test_unknown_primary_is_not_replaced_by_verified_fallback(self):
        """Временная ошибка — не повод менять решение о приоритете источников."""
        updated = cast.apply_portrait_probes(
            {"name": "Lead", "photo_url": self.KP,
             "fallback_photo_urls": [self.COMMONS]},
            self._probes(**{self.KP: "unknown", self.COMMONS: "ok"}),
        )
        self.assertEqual(updated["photo_url"], self.KP)
        self.assertEqual(updated["fallback_photo_urls"], [self.COMMONS])
        self.assertEqual(updated["photo_state"], cast.PHOTO_UNKNOWN)

    def test_rejected_primary_promotes_verified_fallback(self):
        updated = cast.apply_portrait_probes(
            {"name": "Lead", "photo_url": self.KP,
             "fallback_photo_urls": [self.COMMONS]},
            self._probes(**{self.KP: "rejected", self.COMMONS: "ok"}),
        )
        self.assertEqual(updated["photo_url"], self.COMMONS)
        self.assertEqual(updated["fallback_photo_urls"], [])
        self.assertEqual(updated["photo_state"], cast.PHOTO_VERIFIED)

    def test_a_forbidden_commons_fallback_survives_next_to_a_working_primary(self):
        """Ровно тот случай, что портил каталог: основной портрет живой, запасной
        отвечает 403 — и запасной молча исчезал из строки."""
        updated = cast.apply_portrait_probes(
            {"name": "Lead", "photo_url": self.KP,
             "fallback_photo_urls": [self.COMMONS]},
            self._probes(**{
                self.KP: "ok",
                self.COMMONS: cast.portrait_http_verdict(403),
            }),
        )
        self.assertEqual(updated["photo_url"], self.KP)
        self.assertEqual(updated["fallback_photo_urls"], [self.COMMONS])
        self.assertEqual(updated["photo_state"], cast.PHOTO_VERIFIED)

    def test_only_a_proven_missing_file_clears_the_portrait(self):
        updated = cast.apply_portrait_probes(
            {"name": "Lead", "photo_url": self.KP,
             "fallback_photo_urls": [self.COMMONS]},
            self._probes(**{
                self.KP: cast.portrait_http_verdict(404),
                self.COMMONS: cast.portrait_http_verdict(410),
            }),
        )
        self.assertIsNone(updated["photo_url"])
        self.assertEqual(updated["fallback_photo_urls"], [])
        self.assertEqual(updated["photo_state"], cast.PHOTO_REJECTED)


class PortraitProbeSessionIdentityTests(unittest.IsolatedAsyncioTestCase):
    """Сессия проверки портретов обязана представляться так же, как wikidata."""

    async def test_portrait_validator_uses_wikimedia_contact_user_agent(self):
        member = {
            "name": "Lead",
            "photo_url": "https://commons.wikimedia.org/test.jpg",
            "fallback_photo_urls": [],
        }
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.__aexit__.return_value = False

        with (
            patch.object(cast_backfill, "probe_portrait", AsyncMock(
                return_value=cast.PortraitProbe("ok", 330, 440, 20_000))),
            patch.object(cast_backfill.aiohttp, "TCPConnector"),
            patch.object(cast_backfill.aiohttp, "ClientSession",
                         return_value=session) as session_cls,
        ):
            result = await cast_backfill.validate_cast_portraits([member])

        self.assertEqual(result[0]["photo_url"], member["photo_url"])
        kwargs = session_cls.call_args.kwargs
        self.assertEqual(kwargs["headers"]["User-Agent"],
                         wikidata.WIKIMEDIA_USER_AGENT)
        # Контакт обязателен: без ссылки на проект Wikimedia отвечает 403.
        self.assertIn("https://", wikidata.WIKIMEDIA_USER_AGENT)


class PortraitPayloadClassifierTests(unittest.TestCase):
    """Разрешение — не то же самое, что пригодность.

    Карточка актёра в интерфейсе — примерно 84×128 CSS-пикселя. Портрет
    120×190 её перекрывает и годится к показу; заглушка кинопоиска при этом
    крупнее — 208×304. Значит, различать их обязан не размер, а содержимое.
    """

    def _classify(self, **kwargs):
        base = dict(
            url="https://st.kp.yandex.net/photo.jpg",
            content_type="image/jpeg",
            byte_count=12_000,
            dimensions=(330, 440),
            sha256="unique-real-photo",
        )
        return cast_backfill.classify_portrait_payload(**{**base, **kwargs})

    def test_real_120x190_portrait_is_usable(self):
        result = self._classify(
            url="https://st.kp.yandex.net/real.jpg",
            byte_count=12_000, dimensions=(120, 190), sha256="real-photo")
        self.assertEqual(result.verdict, "ok")
        self.assertEqual(result.reason, "usable_portrait")

    def test_real_150x210_portrait_is_usable(self):
        result = self._classify(
            byte_count=14_000, dimensions=(150, 210), sha256="real-photo-2")
        self.assertEqual(result.verdict, "ok")

    def test_low_information_kinopoisk_card_is_rejected(self):
        """Реальные размеры и вес серой «К» из каталога."""
        result = self._classify(
            url="https://st.kp.yandex.net/placeholder.jpg", content_type="image/gif",
            byte_count=2401, dimensions=(208, 304), sha256="not-in-known-set")
        self.assertEqual(result.verdict, "rejected")
        self.assertEqual(result.reason, "low_information_kinopoisk_placeholder")

    def test_the_audited_placeholder_hash_is_rejected_even_when_metadata_looks_fine(self):
        known = next(iter(cast_backfill._KNOWN_KINOPOISK_PLACEHOLDER_SHA256))
        result = self._classify(byte_count=48_000, dimensions=(360, 549), sha256=known)
        self.assertEqual(result.verdict, "rejected")
        self.assertEqual(result.reason, "known_kinopoisk_placeholder")

    def test_real_photo_with_placeholder_dimensions_is_not_rejected(self):
        result = self._classify(
            url="https://st.kp.yandex.net/real-208x304.jpg",
            byte_count=24_000, dimensions=(208, 304), sha256="real-photo-3")
        self.assertEqual(result.verdict, "ok")

    def test_below_display_floor_is_unknown_not_rejected(self):
        result = self._classify(
            url="https://st.kp.yandex.net/tiny-real.jpg",
            byte_count=8_000, dimensions=(70, 105), sha256="tiny-real")
        self.assertEqual(result.verdict, "unknown")
        self.assertEqual(result.reason, "below_display_floor")

    def test_unusual_aspect_ratio_is_unknown_not_rejected(self):
        result = self._classify(byte_count=30_000, dimensions=(900, 300))
        self.assertEqual(result.verdict, "unknown")
        self.assertEqual(result.reason, "unusual_aspect_ratio")

    def test_a_thin_density_rule_never_touches_another_provider(self):
        """Порог плотности — про конкретную заглушку кинопоиска, а не про всех."""
        result = self._classify(
            url="https://commons.wikimedia.org/w/portrait.jpg",
            byte_count=2401, dimensions=(208, 304), sha256="commons-thin")
        self.assertEqual(result.verdict, "ok")

    def test_unreadable_header_and_wrong_type_stay_apart(self):
        unreadable = self._classify(dimensions=None)
        self.assertEqual(unreadable.verdict, "unknown")
        self.assertEqual(unreadable.reason, "dimensions_unavailable")
        wrong_type = self._classify(content_type="text/html")
        self.assertEqual(wrong_type.verdict, "rejected")
        self.assertEqual(wrong_type.reason, "unsupported_content_type")
        tiny = self._classify(byte_count=900)
        self.assertEqual(tiny.verdict, "rejected")
        self.assertEqual(tiny.reason, "file_too_small")

    def test_density_is_reported_for_diagnostics(self):
        result = self._classify(byte_count=12_000, dimensions=(120, 190))
        self.assertAlmostEqual(result.bytes_per_pixel, 12_000 / (120 * 190), places=6)

    def test_the_audited_distributions_do_not_overlap(self):
        """Замер каталога: заглушка 0.0380, живые портреты 0.1940…0.3665."""
        threshold = cast_backfill._KINOPOISK_PLACEHOLDER_MAX_BYTES_PER_PIXEL
        self.assertLess(0.0380, threshold)
        self.assertLess(threshold, 0.1940)

    def test_a_positional_probe_call_still_works(self):
        probe = cast_model.PortraitProbe("ok", 330, 440, 20_000)
        self.assertEqual((probe.width, probe.height, probe.byte_count), (330, 440, 20_000))
        self.assertIsNone(probe.reason)
        self.assertIsNone(probe.bytes_per_pixel)


class BackfillCliLoggingTests(unittest.TestCase):
    """Диагностика портретов бесполезна, если её не видно.

    `validate_cast_portraits()` объясняет каждый не-ok вердикт через
    logger.info. Корневой уровень по умолчанию — WARNING, поэтому у команды,
    запущенной руками, эти строки пропадали. Настраивать логирование при
    ИМПОРТЕ при этом нельзя: модуль импортирует приложение, и библиотека не
    имеет права молча переопределять настройки чужого процесса.
    """

    def test_importing_the_module_leaves_the_root_logger_alone(self):
        with patch("logging.basicConfig") as basic_config:
            importlib.reload(cast_backfill)
        basic_config.assert_not_called()

    def test_running_the_command_turns_info_logs_on(self):
        with (
            patch("logging.basicConfig") as basic_config,
            patch.object(cast_backfill, "_run", new=AsyncMock(return_value=0)) as run,
            patch("sys.argv", ["cast_backfill", "--film-id", "26", "--dry-run"]),
        ):
            self.assertEqual(cast_backfill.main(), 0)
        run.assert_awaited_once()
        self.assertEqual(basic_config.call_args.kwargs["level"], logging.INFO)
