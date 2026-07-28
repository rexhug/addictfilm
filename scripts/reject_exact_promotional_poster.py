"""Inspect or reject the exact current promotional poster for The Covenant.

The operation deliberately has two phases.  First inspect the unique catalog
row and copy its current URL.  Applying requires that exact URL, so a provider
refresh between review and mutation aborts instead of rejecting new artwork.

Production usage (from ``/app/backend``):

    python ../scripts/reject_exact_promotional_poster.py
    python ../scripts/reject_exact_promotional_poster.py \
        --apply --expected-poster-url 'https://reviewed.example/poster.jpg'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import aiosqlite
import database as db
import db_runtime

TARGET_TITLE = "Переводчик"
TARGET_ORIGINAL_TITLE = "The Covenant"
REJECT_REASON = "embedded_kinopoisk_promotion"


async def locate_target() -> dict:
    """Find the target by both exact titles; never rely on a guessed film ID."""
    async with db_runtime.connect(db.DB_PATH, db.DATABASE_URL) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT id, title, title_original, poster_url, poster_display_state, "
            "poster_display_url, poster_reject_reason "
            "FROM films WHERE lower(trim(title)) = lower(?) "
            "AND lower(trim(title_original)) = lower(?)",
            (TARGET_TITLE, TARGET_ORIGINAL_TITLE),
        )).fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"Expected exactly one {TARGET_TITLE!r} / {TARGET_ORIGINAL_TITLE!r} row; "
            f"found {len(rows)}"
        )
    return dict(rows[0])


async def reject_exact_current_poster(expected_poster_url: str) -> dict:
    expected = str(expected_poster_url or "").strip()
    if not expected:
        raise ValueError("--expected-poster-url is required with --apply")
    before = await locate_target()
    current = str(before.get("poster_url") or "").strip()
    if not current:
        raise RuntimeError("Target film has no current poster_url")
    if current != expected:
        raise RuntimeError(
            "Current poster changed after review; refusing to reject it. "
            f"Expected {expected!r}, found {current!r}"
        )
    updated = await db.update_film_poster_display(
        int(before["id"]), state="rejected", reason=REJECT_REASON)
    if not updated or str(updated.get("poster_display_url") or "").strip() != expected:
        raise RuntimeError("Exact poster rejection was not persisted")
    return {
        key: updated.get(key)
        for key in (
            "id", "title", "title_original", "poster_url",
            "poster_display_state", "poster_display_url", "poster_reject_reason",
        )
    }


async def run(*, apply: bool, expected_poster_url: str | None) -> dict:
    await db.init_db()
    if apply:
        return await reject_exact_current_poster(expected_poster_url or "")
    return await locate_target()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect/reject the exact reviewed The Covenant poster")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-poster-url")
    args = parser.parse_args()
    result = asyncio.run(
        run(apply=args.apply, expected_poster_url=args.expected_poster_url))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
