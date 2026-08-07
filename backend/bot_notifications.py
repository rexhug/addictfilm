"""Small, standalone Telegram notification worker.

Pair lifecycle events continue to use :mod:`pair_notifications` as their
durable outbox.  This module provides the scheduled, non-request work that
can be run from cron or a worker process without importing the FastAPI app.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

import aiohttp
import database as db
import db_session
from config import BOT_TOKEN
from pair_notifications import replay_pending_pair_events, retry_notification_deliveries

logger = logging.getLogger(__name__)


async def _telegram_call(method: str, payload: dict) -> bool:
    """Send one Bot API request; missing configuration is a safe no-op."""
    if not BOT_TOKEN:
        return False
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload) as response:
            if response.status >= 400:
                logger.warning("Telegram %s failed with HTTP %s", method, response.status)
                return False
            return True


async def send_message(chat_id: int, text: str, *, reply_markup: dict | None = None) -> bool:
    payload = {"chat_id": int(chat_id), "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _telegram_call("sendMessage", payload)


async def send_photo(chat_id: int, photo: str, caption: str = "") -> bool:
    return await _telegram_call("sendPhoto", {"chat_id": int(chat_id), "photo": photo, "caption": caption})


def reminder_text(films: Iterable[dict], language: str = "ru") -> str:
    titles = [str(item.get("title") or "").strip() for item in films]
    titles = [title for title in titles if title]
    if language == "en":
        prefix = "You watched these films but have not rated them yet:"
    else:
        prefix = "Ты посмотрел(а) эти фильмы, но ещё не поставил(а) оценку:"
    return prefix + "\n" + "\n".join(f"• {title}" for title in titles)


async def send_unrated_reminders(*, since_days: int = 30, limit: int = 10) -> int:
    """Send at most one compact reminder per eligible user."""
    if not BOT_TOKEN:
        return 0
    async with db_session.connect() as connection:
        rows = await (await connection.execute(
            "SELECT id FROM users ORDER BY id")).fetchall()
    sent = 0
    for row in rows:
        user_id = int(row["id"] if hasattr(row, "keys") else row[0])
        settings = await db.get_notification_settings(user_id)
        if not settings["telegram_enabled"]:
            continue
        films = await db.get_unrated_watched(user_id, since_days=since_days, limit=limit)
        if not films:
            continue
        if await send_message(user_id, reminder_text(films, settings["language"]),):
            sent += 1
    return sent


async def run_once() -> dict[str, int]:
    """Run the durable pair outbox and scheduled reminders once."""
    replayed = len(await replay_pending_pair_events())
    retried = await retry_notification_deliveries()
    reminders = await send_unrated_reminders()
    return {"pair_events": replayed, "telegram_retries": retried, "unrated_reminders": reminders}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("notification worker: %s", asyncio.run(run_once()))


if __name__ == "__main__":  # pragma: no cover
    main()
