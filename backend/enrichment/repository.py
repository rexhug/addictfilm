"""Хранение версионированных профилей и ручных правок."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite
import database as db
import db_runtime
import mood
from config import DATABASE_URL

from .models import ExtractedMovieFeatures, PROFILE_STALE
from .taxonomy import ContentType

_MOOD_COLUMNS = (*mood.MOOD_DIMENSIONS, "peak_darkness")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value, default):
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


async def save_profile(film_id: int, features: ExtractedMovieFeatures, *, status: str,
                       source_hash: str, feature_version: str, taxonomy_version: str,
                       extractor_version: str, semantic_model_version: str | None,
                       validation_level: str, warnings: tuple[str, ...]) -> None:
    """Атомарная замена профиля.

    Старый профиль живёт до успешного пересчёта — стирать его заранее значит
    выключить фильм из рекомендаций на время работы воркера.
    """
    now = _now()
    values = {dim: float(features.mood.get(dim, 0.5)) for dim in _MOOD_COLUMNS}
    evidence = json.dumps([item.as_dict() for item in features.evidence],
                          ensure_ascii=False, separators=(",", ":"))
    columns = ", ".join(_MOOD_COLUMNS)
    marks = ", ".join("?" for _ in _MOOD_COLUMNS)
    updates = ", ".join(f"{dim}=?" for dim in _MOOD_COLUMNS)
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        cur = await conn.execute(
            f"UPDATE movie_recommendation_profiles SET status=?, content_type=?, "
            f"feature_version=?, taxonomy_version=?, extractor_version=?, "
            f"semantic_model_version=?, source_hash=?, genres=?, themes=?, tones=?, "
            f"audience_flags=?, {updates}, confidence=?, validation_level=?, warnings=?, "
            f"evidence=?, calculated_at=?, updated_at=? WHERE film_id=?",
            (status, str(features.content_type), feature_version, taxonomy_version,
             extractor_version, semantic_model_version, source_hash,
             json.dumps(sorted(features.genres), ensure_ascii=False),
             json.dumps(sorted(features.themes), ensure_ascii=False),
             json.dumps(sorted(features.tones), ensure_ascii=False),
             json.dumps(sorted(features.audience_flags), ensure_ascii=False),
             *[values[dim] for dim in _MOOD_COLUMNS],
             float(features.confidence), validation_level,
             json.dumps(list(warnings), ensure_ascii=False), evidence, now, now, film_id))
        if not cur.rowcount:
            await conn.execute(
                f"INSERT INTO movie_recommendation_profiles (film_id, status, content_type, "
                f"feature_version, taxonomy_version, extractor_version, semantic_model_version, "
                f"source_hash, genres, themes, tones, audience_flags, {columns}, confidence, "
                f"validation_level, warnings, evidence, calculated_at, updated_at) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,{marks},?,?,?,?,?,?)",
                (film_id, status, str(features.content_type), feature_version, taxonomy_version,
                 extractor_version, semantic_model_version, source_hash,
                 json.dumps(sorted(features.genres), ensure_ascii=False),
                 json.dumps(sorted(features.themes), ensure_ascii=False),
                 json.dumps(sorted(features.tones), ensure_ascii=False),
                 json.dumps(sorted(features.audience_flags), ensure_ascii=False),
                 *[values[dim] for dim in _MOOD_COLUMNS],
                 float(features.confidence), validation_level,
                 json.dumps(list(warnings), ensure_ascii=False), evidence, now, now))
        await conn.commit()


def _row_to_profile(row) -> dict:
    return {
        "film_id": row["film_id"], "status": row["status"], "content_type": row["content_type"],
        "feature_version": row["feature_version"], "taxonomy_version": row["taxonomy_version"],
        "extractor_version": row["extractor_version"],
        "semantic_model_version": row["semantic_model_version"],
        "source_hash": row["source_hash"],
        "genres": _loads(row["genres"], []), "themes": _loads(row["themes"], []),
        "tones": _loads(row["tones"], []), "audience_flags": _loads(row["audience_flags"], []),
        "mood": {dim: float(row[dim]) for dim in _MOOD_COLUMNS},
        "confidence": float(row["confidence"]),
        "validation_level": row["validation_level"],
        "warnings": _loads(row["warnings"], []),
        "calculated_at": row["calculated_at"],
    }


async def get_profile(film_id: int) -> dict | None:
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(
            "SELECT * FROM movie_recommendation_profiles WHERE film_id=?", (film_id,))).fetchone()
    return _row_to_profile(row) if row else None


async def get_profiles(film_ids) -> dict[int, dict]:
    """Профили пачкой: подбор не должен делать запрос на каждый фильм."""
    ids = [int(film_id) for film_id in film_ids]
    if not ids:
        return {}
    out: dict[int, dict] = {}
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            rows = await (await conn.execute(
                f"SELECT * FROM movie_recommendation_profiles WHERE film_id IN ({marks})",
                tuple(chunk))).fetchall()
            out.update({row["film_id"]: _row_to_profile(row) for row in rows})
    return out


async def mark_stale(feature_version: str, taxonomy_version: str) -> int:
    """Профили других версий помечаем устаревшими, но НЕ удаляем: до пересчёта
    они продолжают работать."""
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        cur = await conn.execute(
            "UPDATE movie_recommendation_profiles SET status=?, updated_at=? "
            "WHERE (feature_version<>? OR taxonomy_version<>?) AND status<>?",
            (PROFILE_STALE, _now(), feature_version, taxonomy_version, PROFILE_STALE))
        await conn.commit()
        return cur.rowcount


async def get_override(film_id: int) -> dict | None:
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(
            "SELECT override_data FROM movie_recommendation_profile_overrides WHERE film_id=?",
            (film_id,))).fetchone()
    return _loads(row["override_data"], None) if row else None


async def set_override(film_id: int, data: dict, *, reason: str, created_by: int) -> None:
    now = _now()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        cur = await conn.execute(
            "UPDATE movie_recommendation_profile_overrides SET override_data=?, reason=?, "
            "updated_at=? WHERE film_id=?", (payload, reason[:200], now, film_id))
        if not cur.rowcount:
            await conn.execute(
                "INSERT INTO movie_recommendation_profile_overrides "
                "(film_id, override_data, reason, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)", (film_id, payload, reason[:200], created_by, now, now))
        await conn.commit()


async def delete_override(film_id: int) -> bool:
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        cur = await conn.execute(
            "DELETE FROM movie_recommendation_profile_overrides WHERE film_id=?", (film_id,))
        await conn.commit()
        return bool(cur.rowcount)


async def distribution() -> dict:
    """Сводка для наблюдаемости и админской диагностики."""
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        by_status = await (await conn.execute(
            "SELECT status, COUNT(*) AS n FROM movie_recommendation_profiles "
            "GROUP BY status")).fetchall()
        by_type = await (await conn.execute(
            "SELECT content_type, COUNT(*) AS n FROM movie_recommendation_profiles "
            "GROUP BY content_type")).fetchall()
        average = await (await conn.execute(
            "SELECT AVG(confidence) AS c FROM movie_recommendation_profiles")).fetchone()
        total = await (await conn.execute("SELECT COUNT(*) AS n FROM films")).fetchone()
    return {
        "catalog": total["n"],
        "by_status": {row["status"]: row["n"] for row in by_status},
        "by_content_type": {row["content_type"]: row["n"] for row in by_type},
        "average_confidence": round(float(average["c"] or 0), 4),
    }


async def exceptions(limit: int = 50) -> list[dict]:
    """Очередь ручного разбора: только то, с чем автоматика не справилась.

    Обычный новый фильм сюда не попадает — иначе «ручной разбор исключений»
    превращается в ручную разметку каталога.
    """
    async with db_runtime.connect(db.DB_PATH, DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT p.*, f.title FROM movie_recommendation_profiles p "
            "JOIN films f ON f.id = p.film_id "
            "WHERE p.status IN ('low_confidence','failed') OR p.validation_level<>'valid' "
            "OR p.content_type='unknown' ORDER BY p.confidence ASC LIMIT ?", (limit,))).fetchall()
        dead = await (await conn.execute(
            "SELECT j.film_id, f.title, j.last_error_code, j.attempts FROM movie_enrichment_jobs j "
            "JOIN films f ON f.id = j.film_id WHERE j.status IN ('dead_letter','failed') "
            "ORDER BY j.updated_at DESC LIMIT ?", (limit,))).fetchall()
    items = [{**_row_to_profile(row), "title": row["title"], "kind": "profile"} for row in rows]
    items += [{"film_id": row["film_id"], "title": row["title"], "kind": "job",
               "last_error_code": row["last_error_code"], "attempts": row["attempts"]}
              for row in dead]
    return items


def profile_content_type(profile: dict | None) -> ContentType:
    if not profile:
        return ContentType.UNKNOWN
    try:
        return ContentType(profile["content_type"])
    except ValueError:
        return ContentType.UNKNOWN
