"""Canonical, provider-ordered movie cast and portrait fallback helpers.

The order, names and roles in a canonical cast belong to the primary provider.
Enrichment sources may add stable IDs and portrait candidates, but never alter
those movie-specific billing facts.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

PHOTO_MISSING = "missing"
PHOTO_UNVERIFIED = "unverified"
PHOTO_VERIFIED = "verified"
PHOTO_REJECTED = "rejected"
PHOTO_UNKNOWN = "unknown"

PHOTO_STATES = frozenset({
    PHOTO_MISSING,
    PHOTO_UNVERIFIED,
    PHOTO_VERIFIED,
    PHOTO_REJECTED,
    PHOTO_UNKNOWN,
})

CAST_LIMIT_STORED = 12
CAST_LIMIT_DISPLAYED = 10


def normalize_whitespace(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize_person_name(value: object) -> str:
    text = normalize_whitespace(value).casefold()
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def valid_http_url(value: object) -> str | None:
    url = str(value or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def unique_urls(values: list[object] | tuple[object, ...]) -> list[str]:
    result: list[str] = []
    for value in values:
        url = valid_http_url(value)
        if url and url not in result:
            result.append(url)
    return result


@dataclass(frozen=True, slots=True)
class CastMember:
    person_id: str | None
    wikidata_id: str | None
    name: str
    original_name: str | None
    character: str | None
    billing_order: int
    source: str
    photo_url: str | None
    fallback_photo_urls: tuple[str, ...]
    photo_state: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["fallback_photo_urls"] = list(self.fallback_photo_urls)
        return value


@dataclass(frozen=True, slots=True)
class PortraitProbe:
    verdict: str
    width: int | None = None
    height: int | None = None
    byte_count: int | None = None
    # Почему вынесен именно такой вердикт. Без этого разбор «почему у актёра
    # пропало фото» превращается в ручное скачивание файлов: по одному слову
    # «rejected» нельзя отличить заглушку провайдера от запрета доступа.
    reason: str | None = None
    bytes_per_pixel: float | None = None


def normalize_cast_member(raw: object, *, fallback_order: int) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = normalize_whitespace(raw.get("name"))
    if not name:
        return None
    try:
        billing_order = int(raw.get("billing_order", fallback_order))
    except (TypeError, ValueError):
        billing_order = fallback_order
    photo_url = valid_http_url(raw.get("photo_url"))
    raw_fallbacks = raw.get("fallback_photo_urls")
    fallback_urls = unique_urls(
        list(raw_fallbacks) if isinstance(raw_fallbacks, (list, tuple)) else []
    )
    fallback_urls = [url for url in fallback_urls if url != photo_url]
    state = str(raw.get("photo_state") or (
        PHOTO_UNVERIFIED if photo_url else PHOTO_MISSING
    ))
    if state not in PHOTO_STATES:
        state = PHOTO_UNVERIFIED if photo_url else PHOTO_MISSING
    return CastMember(
        person_id=normalize_whitespace(raw.get("person_id")) or None,
        wikidata_id=normalize_whitespace(raw.get("wikidata_id")) or None,
        name=name,
        original_name=normalize_whitespace(raw.get("original_name")) or None,
        character=normalize_whitespace(raw.get("character")) or None,
        billing_order=max(0, billing_order),
        source=normalize_whitespace(raw.get("source")) or "catalog",
        photo_url=photo_url,
        fallback_photo_urls=tuple(fallback_urls),
        photo_state=state,
    ).to_dict()


def normalize_cast(value: object, *, limit: int = CAST_LIMIT_STORED) -> list[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(value, list):
        return []
    normalized: list[dict] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for fallback_order, raw in enumerate(value):
        member = normalize_cast_member(raw, fallback_order=fallback_order)
        if not member:
            continue
        person_id = member.get("person_id")
        wikidata_id = member.get("wikidata_id")
        name_key = normalize_person_name(
            member.get("original_name") or member.get("name")
        )
        stable_id = (
            f"kp:{person_id}" if person_id
            else f"wd:{wikidata_id}" if wikidata_id
            else None
        )
        if stable_id:
            if stable_id in seen_ids:
                continue
            seen_ids.add(stable_id)
        elif name_key:
            if name_key in seen_names:
                continue
            seen_names.add(name_key)
        normalized.append(member)
    normalized.sort(key=lambda member: (
        int(member["billing_order"]),
        normalize_person_name(member["name"]),
    ))
    return normalized[:max(1, limit)]


def decode_cast(value: str | None) -> list[dict]:
    return normalize_cast(value)


def legacy_actor_photos(canonical: list[dict]) -> list[dict]:
    result: list[dict] = []
    for member in normalize_cast(canonical)[:CAST_LIMIT_DISPLAYED]:
        item = {
            "person_id": member.get("person_id"),
            "name": member["name"],
            "photo_url": member.get("photo_url"),
            "source": member.get("source") or "catalog",
        }
        fallbacks = unique_urls(member.get("fallback_photo_urls") or [])
        if fallbacks:
            item["fallback_photo_urls"] = fallbacks
        result.append(item)
    return result


def _unique_index(members: list[dict], field: str) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for member in members:
        key = normalize_person_name(member.get(field))
        if key:
            grouped.setdefault(key, []).append(member)
    return {key: values[0] for key, values in grouped.items() if len(values) == 1}


def merge_portrait_fallbacks(
    canonical: list[dict], enrichment: list[dict],
) -> list[dict]:
    base = normalize_cast(canonical)
    extra = normalize_cast(enrichment)
    if not base:
        return extra
    by_wikidata = {
        member["wikidata_id"]: member
        for member in extra
        if member.get("wikidata_id")
    }
    by_original = _unique_index(extra, "original_name")
    by_name = _unique_index(extra, "name")
    result: list[dict] = []
    for original in base:
        member = dict(original)
        match = None
        if member.get("wikidata_id"):
            match = by_wikidata.get(member["wikidata_id"])
        original_key = normalize_person_name(member.get("original_name"))
        name_key = normalize_person_name(member.get("name"))
        if match is None and original_key:
            match = by_original.get(original_key) or by_name.get(original_key)
        if match is None and name_key:
            match = by_name.get(name_key) or by_original.get(name_key)
        urls = unique_urls([
            member.get("photo_url"),
            *(member.get("fallback_photo_urls") or []),
            match.get("photo_url") if match else None,
            *((match.get("fallback_photo_urls") or []) if match else []),
        ])
        member["photo_url"] = urls[0] if urls else None
        member["fallback_photo_urls"] = urls[1:]
        if match and not member.get("wikidata_id"):
            member["wikidata_id"] = match.get("wikidata_id")
        if member["photo_url"]:
            if member.get("photo_state") in {PHOTO_MISSING, PHOTO_REJECTED}:
                member["photo_state"] = PHOTO_UNVERIFIED
        else:
            member["photo_state"] = PHOTO_MISSING
        result.append(member)
    return normalize_cast(result)


def portrait_http_verdict(status: int) -> str:
    if status == 200:
        return "inspect"
    # Только эти статусы доказывают, что картинки больше нет.
    if status in {404, 410}:
        return "rejected"
    # Аутентификация, анти-бот политика, throttling и отказ сервера НЕ
    # доказывают, что сохранённая ссылка испорчена. Такой кандидат остаётся
    # пригодным для повторной проверки: Wikimedia отвечает 403 на запрос без
    # контактного User-Agent, и раньше это стирало живые портреты.
    if status in {401, 403, 408, 425, 429} or 500 <= status <= 599:
        return "unknown"
    return "rejected"


def apply_portrait_probes(
    member: dict, probes: dict[str, PortraitProbe],
) -> dict:
    """Отсеять доказанно битые ссылки, СОХРАНИВ порядок остальных.

    Порядок кандидатов — это решение о приоритете источников, принятое раньше
    (каталожное фото ведёт, свободное с Commons идёт запасным). Временная
    ошибка не повод его пересматривать: повышать запасной портрет можно только
    тогда, когда основной действительно отвергнут.
    """
    updated = dict(member)
    candidates = unique_urls([
        member.get("photo_url"),
        *(member.get("fallback_photo_urls") or []),
    ])
    survivors: list[tuple[str, str]] = []
    for url in candidates:
        probe = probes.get(url)
        # Непроверенный кандидат — это «неизвестно», а не «плохо».
        verdict = probe.verdict if probe is not None else "unknown"
        if verdict == "rejected":
            continue
        survivors.append((url, verdict))
    if not survivors:
        updated["photo_url"] = None
        updated["fallback_photo_urls"] = []
        updated["photo_state"] = PHOTO_REJECTED
        return updated
    primary_url, primary_verdict = survivors[0]
    updated["photo_url"] = primary_url
    updated["fallback_photo_urls"] = [url for url, _ in survivors[1:]]
    updated["photo_state"] = (
        PHOTO_VERIFIED if primary_verdict == "ok" else PHOTO_UNKNOWN
    )
    return updated


def parse_utc_datetime(value: object) -> datetime | None:
    text = normalize_whitespace(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def cast_refresh_due(film: dict, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    canonical = decode_cast(film.get("cast_json"))
    checked_at = parse_utc_datetime(film.get("cast_checked_at"))
    if not canonical or any(member.get("photo_state") == PHOTO_UNKNOWN for member in canonical):
        ttl = timedelta(hours=6)
    elif any(
        member.get("photo_state") in {PHOTO_MISSING, PHOTO_UNVERIFIED}
        for member in canonical[:CAST_LIMIT_DISPLAYED]
    ):
        ttl = timedelta(days=7)
    else:
        ttl = timedelta(days=30)
    return checked_at is None or now - checked_at >= ttl
