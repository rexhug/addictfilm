"""Durable, localized notifications for pair lifecycle events.

The database inbox is the source of truth. Telegram delivery is deliberately an
asynchronous best-effort channel: a blocked bot, a network timeout or Telegram
rate limit can never roll back accepting, declining or ending a pair.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

import aiohttp

import database as db
from config import BOT_TOKEN

logger = logging.getLogger(__name__)
BOT_USERNAME = os.getenv("BOT_USERNAME", "addictfilmbot").lstrip("@")
_tasks: set[asyncio.Task] = set()


_TEXT = {
    "ru": {
        "pair.invite.created": ("Приглашение в пару", "{actor} приглашает тебя отмечать фильмы вместе.", "Открыть"),
        "pair.invite.accepted": ("Теперь вы в паре", "{actor} принял(а) приглашение. Ваша общая статистика уже готова.", "Смотреть статистику"),
        "pair.invite.declined": ("Приглашение отклонено", "{actor} пока не присоединился(ась) к паре.", "Открыть"),
        "pair.invite.cancelled": ("Приглашение отменено", "{actor} отменил(а) приглашение в пару.", "Открыть"),
        "pair.invite.expired": ("Приглашение истекло", "Приглашение в пару больше не активно.", "Открыть"),
        "pair.ended.actor": ("Пара завершена", "Вы завершили пару с {partner}.", "Открыть"),
        "pair.ended.partner": ("Пара завершена", "{actor} завершил(а) вашу пару.", "Открыть"),
    },
    "en": {
        "pair.invite.created": ("Pair invitation", "{actor} invited you to track films together.", "Open"),
        "pair.invite.accepted": ("You're now paired", "{actor} accepted the invite. Your shared stats are ready.", "View stats"),
        "pair.invite.declined": ("Invitation declined", "{actor} did not join the pair right now.", "Open"),
        "pair.invite.cancelled": ("Invitation cancelled", "{actor} cancelled the pair invitation.", "Open"),
        "pair.invite.expired": ("Invitation expired", "The pair invitation is no longer active.", "Open"),
        "pair.ended.actor": ("Pair ended", "You ended your pair with {partner}.", "Open"),
        "pair.ended.partner": ("Pair ended", "{actor} ended your pair.", "Open"),
    },
}


def _startapp_url(deep_link: str | None) -> str:
    suffix = (deep_link or "").strip().lstrip("_")
    return f"https://t.me/{BOT_USERNAME}?startapp={suffix}" if suffix else f"https://t.me/{BOT_USERNAME}"


def _copy(language: str, event_type: str, *, is_actor: bool, actor_name: str, partner_name: str) -> dict:
    language = language if language in _TEXT else "ru"
    key = "pair.ended.actor" if event_type == "pair.ended" and is_actor else (
        "pair.ended.partner" if event_type == "pair.ended" else event_type)
    title, body, action_label = _TEXT[language].get(key, _TEXT[language]["pair.invite.created"])
    values = {"actor": actor_name or ("Партнёр" if language == "ru" else "Your partner"),
              "partner": partner_name or ("партнёром" if language == "ru" else "your partner")}
    return {"title": title.format(**values), "body": body.format(**values), "action_label": action_label}


async def _send_telegram(*, recipient_id: int, text: dict, deep_link: str | None) -> None:
    if not BOT_TOKEN:
        return
    body = {
        "chat_id": recipient_id,
        "text": f"{text['title']}\n\n{text['body']}",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [[{"text": text["action_label"], "url": _startapp_url(deep_link)}]]},
    }
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=body) as response:
            if response.status >= 400:
                detail = (await response.text())[:300]
                raise RuntimeError(f"Telegram {response.status}: {detail}")


async def _deliver(notification_id: int, recipient_id: int, text: dict, deep_link: str | None) -> None:
    for attempt in range(2):
        try:
            await _send_telegram(recipient_id=recipient_id, text=text, deep_link=deep_link)
        except Exception as exc:  # bot delivery is not allowed to break pair actions
            message = str(exc)
            # Retry only a transport error, 429, or a temporary Telegram 5xx.
            # A blocked bot (403) is final and must not waste a user's request.
            retryable = not message.startswith("Telegram ") or message.startswith("Telegram 429") or message.startswith("Telegram 5")
            if retryable and attempt == 0:
                await asyncio.sleep(1)
                continue
            logger.info("Telegram notification %s for %s failed: %s", notification_id, recipient_id, exc)
            await db.finish_notification_delivery(notification_id, channel="telegram", sent=False, error=message)
            return
        else:
            await db.finish_notification_delivery(notification_id, channel="telegram", sent=True)
            return


def _schedule(coro) -> None:
    task = asyncio.create_task(coro, name="pair-telegram-notification")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def emit_pair_event(*, event_type: str, entity_id: str, actor_id: int | None,
                          recipient_ids: Iterable[int], deep_link: str | None,
                          telegram: bool = True) -> None:
    """Create idempotent inbox rows and schedule opt-in Telegram delivery."""
    unique_recipients = sorted({int(user_id) for user_id in recipient_ids if user_id is not None})
    if not unique_recipients:
        return
    actor = await db.get_user(actor_id) if actor_id is not None else None
    actor_name = (actor or {}).get("first_name") or (actor or {}).get("username") or ""
    for recipient_id in unique_recipients:
        recipient = await db.get_user(recipient_id)
        if not recipient:
            continue
        settings = await db.get_notification_settings(recipient_id)
        partner_name = actor_name
        if event_type == "pair.ended" and actor_id == recipient_id:
            # The person who ended a pair sees the other participant's name.
            partner_name = ""
        copy = _copy(settings["language"], event_type, is_actor=actor_id == recipient_id,
                     actor_name=actor_name, partner_name=partner_name)
        payload = {**copy, "event_type": event_type}
        notification_id, created = await db.create_notification(
            event_type=event_type, recipient_id=recipient_id, actor_id=actor_id,
            entity_id=entity_id, payload=payload, deep_link=deep_link,
            idempotency_key=f"{event_type}:{entity_id}:{recipient_id}:inapp")
        if not created or not telegram or not settings["telegram_enabled"] or not BOT_TOKEN:
            continue
        created_delivery = await db.create_notification_delivery(
            notification_id, channel="telegram", idempotency_key=f"{event_type}:{entity_id}:{recipient_id}:telegram")
        if created_delivery:
            _schedule(_deliver(notification_id, recipient_id, copy, deep_link))


async def drain() -> None:
    """Stop background delivery gracefully during an app shutdown."""
    if not _tasks:
        return
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
