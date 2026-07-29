import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import cast
import database as db
import kinopoisk
from enrichment.cast_backfill import enrich_film_cast


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
