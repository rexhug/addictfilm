"""Bounded canonical-cast enrichment and operator backfill command."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass

import aiohttp
import cast as cast_model
import database as db
import kinopoisk
import wikidata

from enrichment.hero import _sniff_dimensions

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=12, connect=5)
_PROBE_MAX_BYTES = 2 * 1024 * 1024
_PROBE_MIN_BYTES = 8 * 1024
_ACCEPTED_TYPES = frozenset({
    "image/avif", "image/gif", "image/jpeg", "image/png", "image/webp",
})


@dataclass(frozen=True, slots=True)
class CastEnrichmentResult:
    film_id: int
    title: str
    kp_id: str | None
    old_order: tuple[str, ...]
    new_order: tuple[str, ...]
    kinopoisk_cast_count: int
    wikidata_cast_count: int
    person_detail_requests: int
    canonical: tuple[dict, ...]
    wrote: bool

    def as_dict(self) -> dict:
        members = list(self.canonical)
        return {
            "film_id": self.film_id,
            "title": self.title,
            "kp_id": self.kp_id,
            "old_order": list(self.old_order),
            "new_order": list(self.new_order),
            "kinopoisk_cast_count": self.kinopoisk_cast_count,
            "wikidata_cast_count": self.wikidata_cast_count,
            "person_ids": [m.get("person_id") for m in members],
            "primary_photos": [m.get("photo_url") for m in members],
            "fallback_photos": [m.get("fallback_photo_urls") or [] for m in members],
            "verified": sum(m.get("photo_state") == cast_model.PHOTO_VERIFIED for m in members),
            "missing": sum(m.get("photo_state") == cast_model.PHOTO_MISSING for m in members),
            "rejected": sum(m.get("photo_state") == cast_model.PHOTO_REJECTED for m in members),
            "unknown": sum(m.get("photo_state") == cast_model.PHOTO_UNKNOWN for m in members),
            "person_detail_requests": self.person_detail_requests,
            "would_write": not self.wrote,
            "wrote": self.wrote,
        }


async def probe_portrait(
    url: str, *, session: aiohttp.ClientSession,
) -> cast_model.PortraitProbe:
    """Validate a portrait without decoding or altering the source image."""
    try:
        async with session.get(url, allow_redirects=True) as response:
            verdict = cast_model.portrait_http_verdict(response.status)
            if verdict != "inspect":
                return cast_model.PortraitProbe(verdict)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in _ACCEPTED_TYPES:
                return cast_model.PortraitProbe("rejected")
            body = bytearray()
            async for chunk in response.content.iter_chunked(32 * 1024):
                body.extend(chunk)
                if len(body) > _PROBE_MAX_BYTES:
                    return cast_model.PortraitProbe("rejected", byte_count=len(body))
            byte_count = len(body)
            dimensions = _sniff_dimensions(bytes(body[:64 * 1024]))
            if byte_count < _PROBE_MIN_BYTES:
                return cast_model.PortraitProbe("rejected", byte_count=byte_count)
            # AVIF/WebP may not be understood by the intentionally tiny header
            # sniffer. That is inability to verify, not proof of a bad portrait.
            if dimensions is None:
                return cast_model.PortraitProbe("unknown", byte_count=byte_count)
            width, height = dimensions
            aspect = width / height if height else 0
            accepted = width >= 160 and height >= 160 and 0.45 <= aspect <= 1.45
            return cast_model.PortraitProbe(
                "ok" if accepted else "rejected", width, height, byte_count,
            )
    except (TimeoutError, aiohttp.ClientError):
        return cast_model.PortraitProbe("unknown")


async def validate_cast_portraits(canonical: list[dict]) -> list[dict]:
    urls = cast_model.unique_urls([
        url
        for member in canonical
        for url in [
            member.get("photo_url"),
            *(member.get("fallback_photo_urls") or []),
        ]
    ])
    if not urls:
        return canonical
    connector = aiohttp.TCPConnector(limit=4)
    async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT, connector=connector) as session:
        results = await asyncio.gather(*(
            probe_portrait(url, session=session) for url in urls
        ))
    probes = dict(zip(urls, results, strict=True))
    return [cast_model.apply_portrait_probes(member, probes) for member in canonical]


async def enrich_film_cast(
    film: dict,
    *,
    validate_portraits: bool = False,
    person_detail_limit: int = 3,
    dry_run: bool = False,
    wikidata_cast: list[dict] | None = None,
    kinopoisk_document: dict | None = None,
) -> CastEnrichmentResult:
    film_id = int(film["id"])
    kp_id = str(film.get("kp_id") or "").strip()
    imdb_id = str(film.get("imdb_id") or "").strip()
    old = cast_model.decode_cast(film.get("cast_json"))
    canonical = old
    kp_count = 0
    # A persisted Kinopoisk identity is authoritative. A document resolved
    # through IMDb is only a compatibility bridge for older OMDb-created rows.
    document = None
    if kp_id:
        document = await kinopoisk.get_movie(kp_id)
    elif kinopoisk_document is not None:
        document = kinopoisk_document
    if document is None and imdb_id.startswith("tt"):
        document = (
            await kinopoisk.cast_documents_by_imdb([imdb_id])
        ).get(imdb_id)
    if document:
        resolved_kp_id = str(document.get("id") or "").strip()
        if resolved_kp_id:
            kp_id = resolved_kp_id
        extracted = kinopoisk.extract_cast(document.get("persons") or [], limit=12)
        if extracted:
            canonical = extracted
            kp_count = len(extracted)
    if wikidata_cast is None:
        wikidata_cast = []
        if imdb_id.startswith("tt"):
            wikidata_cast = (
                await wikidata.get_cast_by_imdb([imdb_id], max_actors=12)
            ).get(imdb_id, [])
    canonical = (
        cast_model.merge_portrait_fallbacks(canonical, wikidata_cast)
        if canonical else cast_model.normalize_cast(wikidata_cast)
    )
    detail_requests = 0
    limit = max(0, min(person_detail_limit, 3))
    for index, member in enumerate(canonical):
        if detail_requests >= limit:
            break
        if not member.get("person_id") or member.get("photo_url"):
            continue
        detail_requests += 1
        person = await kinopoisk.get_person(member["person_id"])
        photo_url = cast_model.valid_http_url((person or {}).get("photo"))
        if photo_url:
            updated = dict(member)
            updated["photo_url"] = photo_url
            updated["photo_state"] = cast_model.PHOTO_UNVERIFIED
            canonical[index] = updated
    if validate_portraits:
        canonical = await validate_cast_portraits(canonical)
    wrote = False
    if not dry_run:
        wrote = await db.update_film_cast(
            film_id,
            canonical,
            kp_id=kp_id or None,
        )
    return CastEnrichmentResult(
        film_id=film_id,
        title=str(film.get("title") or ""),
        kp_id=kp_id or None,
        old_order=tuple(member["name"] for member in old),
        new_order=tuple(member["name"] for member in canonical),
        kinopoisk_cast_count=kp_count,
        wikidata_cast_count=len(wikidata_cast),
        person_detail_requests=detail_requests,
        canonical=tuple(canonical),
        wrote=wrote,
    )


async def _run(args: argparse.Namespace) -> int:
    try:
        films = await db.films_for_cast_backfill(film_id=args.film_id, limit=args.limit)
        # Два РАЗНЫХ списка, потому что условия у них разные.
        #
        # Документ Kinopoisk нужен только строкам без kp_id: сохранённая
        # идентичность авторитетна, а разрешение через IMDb — лишь мост для
        # старых записей. Wikidata же приносит портреты и нужна КАЖДОМУ фильму
        # с валидным IMDb-идентификатором: наличие kp_id ничего не говорит о
        # том, есть ли у актёра свободная фотография на Commons.
        #
        # Раньше оба запроса шли по одному отфильтрованному списку, поэтому
        # фильмы с kp_id (215 из 295 в каталоге) получали wikidata_cast=[] и
        # оставались вообще без запасных портретов — механизм fallback во
        # фронтенде был подключён, но подавать в него было нечего.
        legacy_imdb_ids = [
            str(film.get("imdb_id") or "")
            for film in films
            if not film.get("kp_id")
            if str(film.get("imdb_id") or "").startswith("tt")
        ]
        wikidata_imdb_ids = [
            str(film.get("imdb_id") or "")
            for film in films
            if str(film.get("imdb_id") or "").startswith("tt")
        ]
        wikidata_map = (
            await wikidata.get_cast_by_imdb(wikidata_imdb_ids, max_actors=12)
            if wikidata_imdb_ids else {}
        )
        kinopoisk_map = (
            await kinopoisk.cast_documents_by_imdb(legacy_imdb_ids)
            if legacy_imdb_ids else {}
        )
        semaphore = asyncio.Semaphore(max(1, min(args.concurrency, 4)))

        async def process(film: dict) -> dict:
            async with semaphore:
                outcome = await enrich_film_cast(
                    film,
                    validate_portraits=args.validate_portraits,
                    person_detail_limit=args.person_detail_limit_per_film,
                    dry_run=args.dry_run,
                    wikidata_cast=wikidata_map.get(str(film.get("imdb_id") or ""), []),
                    kinopoisk_document=(
                        None
                        if film.get("kp_id")
                        else kinopoisk_map.get(str(film.get("imdb_id") or ""))
                    ),
                )
                payload = outcome.as_dict()
                payload.update({
                    "imdb_id": film.get("imdb_id"),
                })
                return payload

        for start in range(0, len(films), args.batch_size):
            batch = films[start:start + args.batch_size]
            for payload in await asyncio.gather(*(process(film) for film in batch)):
                print(json.dumps(payload, ensure_ascii=False))
        return 0
    finally:
        # This module is also a short-lived CLI. Unlike the FastAPI process it
        # has no application shutdown hook, so provider sessions must be
        # released here on success, failure, and cancellation.
        await asyncio.gather(
            kinopoisk.aclose(),
            wikidata.aclose(),
            return_exceptions=True,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill canonical movie cast")
    parser.add_argument("--film-id", type=int)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-portraits", action="store_true")
    parser.add_argument("--person-detail-limit-per-film", type=int, default=3)
    return parser


def main() -> int:
    args = _parser().parse_args()
    args.limit = max(1, min(args.limit, 500))
    args.batch_size = max(1, min(args.batch_size, 50))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
