"""Recipient-aware delivery for durable pair lifecycle events.

The pair action writes one immutable event in the same database transaction as
the state change.  This module only turns that already-committed event into
recipient-specific inbox rows and best-effort Telegram messages.  It never
guesses roles from user order or from the user currently using the Mini App.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiohttp
import database as db
from config import BOT_TOKEN

logger = logging.getLogger(__name__)
BOT_USERNAME = os.getenv("BOT_USERNAME", "addictfilmbot").lstrip("@")
_tasks: set[asyncio.Task] = set()
_DELIVERY_STALE_AFTER_SECONDS = 120


@dataclass(frozen=True)
class RecipientContext:
    """A canonical event viewed by exactly one recipient."""

    event: dict
    recipient_user_id: int
    partner_user_id: int | None
    recipient: dict
    inviter: dict | None
    invitee: dict | None
    actor: dict | None
    partner: dict | None
    language: str


def display_name(user: dict | None, language: str) -> str:
    """Use a stable, human-facing name without guessing grammatical gender."""
    if user:
        return (user.get("display_name") or user.get("first_name") or user.get("username") or "").strip() or _fallback_name(language)
    return _fallback_name(language)


def _fallback_name(language: str) -> str:
    return "Пользователь" if language == "ru" else "User"


def _startapp_url(deep_link: str | None) -> str:
    suffix = (deep_link or "").strip().lstrip("_")
    return f"https://t.me/{BOT_USERNAME}?startapp={suffix}" if suffix else f"https://t.me/{BOT_USERNAME}"


def delivery_channels(context: RecipientContext) -> tuple[str, ...]:
    """The complete channel policy, intentionally independent of API routes."""
    event_type = context.event["event_type"]
    actor_id = context.event.get("actor_user_id")
    recipient_id = context.recipient_user_id
    inviter_id = context.event.get("inviter_user_id")

    if event_type == "pair.invite.accepted":
        # The person who accepted gets a useful in-app confirmation, but never
        # a redundant bot message about their own tap.
        return ("inapp", "telegram") if recipient_id == inviter_id else ("inapp",)
    if event_type in ("pair.invite.declined", "pair.invite.cancelled"):
        return ("inapp", "telegram")
    if event_type == "pair.invite.expired":
        return ("inapp",)
    if event_type == "pair.ended":
        return () if recipient_id == actor_id else ("inapp", "telegram")
    if event_type == "pair.ended.system":
        return ("inapp", "telegram")
    # Creating an invitation is represented by its direct invite card/link.
    return ()


def format_pair_notification(context: RecipientContext) -> dict:
    """Create localized copy from explicit event and recipient roles only."""
    event_type = context.event["event_type"]
    language = context.language if context.language in ("ru", "en") else "ru"
    inviter_name = display_name(context.inviter, language)
    invitee_name = display_name(context.invitee, language)
    partner_name = display_name(context.partner, language)
    recipient_id = context.recipient_user_id
    inviter_id = context.event.get("inviter_user_id")

    if language == "ru":
        if event_type == "pair.invite.accepted":
            return {"title": "Партнёр подключён", "body": f"Теперь твой партнёр — {partner_name}. Общая статистика уже доступна.", "action_label": "Статистика", "deep_link": "stats"}
        if event_type == "pair.invite.declined":
            return {"title": "Приглашение отклонено", "body": f"Твоё приглашение для {invitee_name} отклонено.", "action_label": "Открыть приложение", "deep_link": "stats"}
        if event_type == "pair.invite.cancelled":
            return {"title": "Приглашение отменено", "body": f"Приглашение от {inviter_name} отменено.", "action_label": "Открыть приложение", "deep_link": ""}
        if event_type == "pair.invite.expired":
            body = (f"Приглашение для {invitee_name} больше не активно."
                    if recipient_id == inviter_id and context.invitee
                    else "Твоё приглашение больше не активно." if recipient_id == inviter_id
                    else f"Приглашение от {inviter_name} больше не активно.")
            return {"title": "Приглашение истекло", "body": body, "action_label": "Открыть приложение", "deep_link": ""}
        if event_type == "pair.ended":
            return {"title": "Партнёр отключён", "body": f"Связь с {partner_name} завершена. Общая статистика сохранена.", "action_label": "Открыть", "deep_link": "stats"}
        return {"title": "Партнёр отключён", "body": f"Связь с {partner_name} завершена. Общая статистика сохранена.", "action_label": "Открыть", "deep_link": "stats"}

    if event_type == "pair.invite.accepted":
        return {"title": "Partner connected", "body": f"You are now connected with {partner_name}. Your shared stats are ready.", "action_label": "View stats", "deep_link": "stats"}
    if event_type == "pair.invite.declined":
        return {"title": "Invitation declined", "body": f"{invitee_name} declined your partner invitation.", "action_label": "Open app", "deep_link": "stats"}
    if event_type == "pair.invite.cancelled":
        return {"title": "Invitation cancelled", "body": f"{inviter_name} cancelled the partner invitation.", "action_label": "Open app", "deep_link": ""}
    if event_type == "pair.invite.expired":
        body = (f"Your invitation for {invitee_name} is no longer active."
                if recipient_id == inviter_id and context.invitee
                else "Your invitation is no longer active." if recipient_id == inviter_id
                else f"The invitation from {inviter_name} is no longer active.")
        return {"title": "Invitation expired", "body": body, "action_label": "Open app", "deep_link": ""}
    if event_type == "pair.ended":
        return {"title": "Partner disconnected", "body": f"Your connection with {partner_name} has ended. Your shared stats are saved.", "action_label": "Open", "deep_link": "stats"}
    return {"title": "Partner disconnected", "body": f"Your connection with {partner_name} has ended. Your shared stats are saved.", "action_label": "Open", "deep_link": "stats"}


async def _send_telegram(*, recipient_id: int, text: dict) -> None:
    if not BOT_TOKEN:
        return
    body = {
        "chat_id": recipient_id,
        "text": f"{text['title']}\n\n{text['body']}",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [[{"text": text["action_label"], "url": _startapp_url(text["deep_link"])}]]},
    }
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=body) as response:
            if response.status >= 400:
                raise RuntimeError(f"Telegram {response.status}: {(await response.text())[:300]}")


def _retryable_telegram_error(error: str | None) -> bool:
    """Retry only errors Telegram explicitly confirms were not delivered."""
    message = str(error or "")
    return message.startswith("Telegram 429") or message.startswith("Telegram 5")


def _stale_delivery_cutoff() -> str:
    return (datetime.now(UTC) - timedelta(seconds=_DELIVERY_STALE_AFTER_SECONDS)).isoformat()


def _delivery_text(row: dict) -> dict | None:
    """Restore a retry payload from the immutable inbox notification."""
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        logger.warning("Notification delivery %s has invalid payload", row.get("notification_id"))
        return None
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not title or not body:
        logger.warning("Notification delivery %s has no message copy", row.get("notification_id"))
        return None
    return {
        "title": title,
        "body": body,
        "action_label": str(payload.get("action_label") or "Open"),
        "deep_link": str(row.get("deep_link") or ""),
    }


async def _deliver(notification_id: int, recipient_id: int, text: dict) -> None:
    for attempt in range(2):
        try:
            await _send_telegram(recipient_id=recipient_id, text=text)
        except Exception as exc:  # Delivery must never roll back a pair action.
            message = str(exc)
            if _retryable_telegram_error(message) and attempt == 0:
                await asyncio.sleep(1)
                continue
            logger.info("Telegram notification %s for %s failed: %s", notification_id, recipient_id, exc)
            await db.finish_notification_delivery(notification_id, channel="telegram", sent=False, error=message)
            return
        await db.finish_notification_delivery(notification_id, channel="telegram", sent=True)
        return


def _schedule(coro) -> None:
    task = asyncio.create_task(coro, name="pair-telegram-notification")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _partner_id(event: dict, recipient_id: int) -> int | None:
    """Find the other participant strictly from stored role IDs."""
    candidates = (event.get("inviter_user_id"), event.get("invitee_user_id"), event.get("actor_user_id"))
    for user_id in candidates:
        if user_id is not None and int(user_id) != int(recipient_id):
            return int(user_id)
    return None


async def dispatch_pair_event(*, event: dict) -> None:
    """Deliver a committed event to its immutable, transactionally stored audience."""
    recipient_rows = await db.get_pair_event_recipients(event["id"])
    if not recipient_rows:
        return
    unique_recipients = [row["recipient_user_id"] for row in recipient_rows]
    recipient_partners = {row["recipient_user_id"]: row["partner_user_id"] for row in recipient_rows}
    user_ids = {int(user_id) for user_id in (
        event.get("inviter_user_id"), event.get("invitee_user_id"), event.get("actor_user_id")) if user_id is not None}
    user_ids.update(unique_recipients)
    users = {user_id: await db.get_user(user_id) for user_id in user_ids}
    for recipient_id in unique_recipients:
        recipient = users.get(recipient_id)
        if not recipient:
            # The account may have been deleted after the pair event committed.
            # Acknowledge it so a replay worker does not retry it forever.
            await db.mark_pair_event_recipient_dispatched(event["id"], recipient_id)
            continue
        settings = await db.get_notification_settings(recipient_id)
        partner_id = recipient_partners.get(recipient_id)
        context = RecipientContext(
            event=event, recipient_user_id=recipient_id, partner_user_id=partner_id,
            recipient=recipient, inviter=users.get(event.get("inviter_user_id")),
            invitee=users.get(event.get("invitee_user_id")), actor=users.get(event.get("actor_user_id")),
            partner=users.get(partner_id), language=settings["language"],
        )
        channels = delivery_channels(context)
        if not channels:
            await db.mark_pair_event_recipient_dispatched(event["id"], recipient_id)
            continue
        text = format_pair_notification(context)
        payload = {
            "title": text["title"], "body": text["body"], "action_label": text["action_label"],
            "event_type": event["event_type"],
            "roles": {
                "inviter_user_id": event.get("inviter_user_id"), "invitee_user_id": event.get("invitee_user_id"),
                "actor_user_id": event.get("actor_user_id"), "recipient_user_id": recipient_id,
                "partner_user_id": partner_id, "invitation_id": event.get("invitation_id"),
                "pair_id": event.get("pair_id"), "event_type": event["event_type"],
            },
        }
        notification_id = None
        if "inapp" in channels:
            notification_id, _ = await db.create_notification(
                event_id=event["id"], event_type=event["event_type"], recipient_id=recipient_id,
                actor_id=event.get("actor_user_id"), entity_id=event.get("invitation_id") or event.get("pair_id") or event["id"],
                payload=payload, deep_link=text["deep_link"],
                idempotency_key=f"{event['id']}:{recipient_id}:inapp")
        if "telegram" not in channels or not settings["telegram_enabled"] or not BOT_TOKEN:
            await db.mark_pair_event_recipient_dispatched(event["id"], recipient_id)
            continue
        # Telegram delivery belongs to the canonical event even when the inbox
        # item is intentionally omitted (for example, a system event).
        if notification_id is None:
            notification_id, _ = await db.create_notification(
                event_id=event["id"], event_type=event["event_type"], recipient_id=recipient_id,
                actor_id=event.get("actor_user_id"), entity_id=event.get("invitation_id") or event.get("pair_id") or event["id"],
                payload=payload, deep_link=text["deep_link"],
                idempotency_key=f"{event['id']}:{recipient_id}:telegram-source")
        if await db.create_notification_delivery(notification_id, channel="telegram", idempotency_key=f"{event['id']}:{recipient_id}:telegram"):
            # Reserve before spawning: an hourly recovery sweep must never
            # enqueue a second send while this request is already in flight.
            if await db.claim_notification_delivery(notification_id, channel="telegram",
                                                    stale_before=_stale_delivery_cutoff()):
                _schedule(_deliver(notification_id, recipient_id, text))
        # A queued delivery is durable in notification_deliveries.  The outbox
        # event itself can now be acknowledged even if Telegram is temporarily
        # unavailable; _deliver records the retryable failure without rolling
        # back a valid pair action.
        await db.mark_pair_event_recipient_dispatched(event["id"], recipient_id)


async def replay_pending_pair_events(limit: int = 100) -> int:
    """Recover events committed just before a process restart or crash."""
    events = await db.list_pending_pair_events(limit=limit)
    for event in events:
        await dispatch_pair_event(event=event)
    return len(events)


async def retry_notification_deliveries(limit: int = 100) -> int:
    """Resume only Telegram sends known not to have been delivered.

    Pair events themselves can be acknowledged immediately because their inbox
    records and delivery rows are durable.  This worker is the matching second
    half: it retries explicit Telegram 429/5xx failures. In-flight sends with
    an unknown outcome are archived, not replayed, so a deploy cannot spam a
    person with old bot messages.
    """
    stale_before = _stale_delivery_cutoff()
    await db.abandon_stale_notification_deliveries(stale_before=stale_before)
    rows = await db.list_recoverable_notification_deliveries(stale_before=stale_before, limit=limit)
    scheduled = 0
    for row in rows:
        if row["status"] == "failed" and not _retryable_telegram_error(row.get("last_error")):
            continue
        text = _delivery_text(row)
        if text is None:
            await db.finish_notification_delivery(
                int(row["notification_id"]), channel="telegram", sent=False,
                # This is a malformed local record, not a temporary outage.
                # Mark it as a terminal Telegram 4xx-style failure so the
                # recurring recovery worker cannot retry it forever.
                error="Telegram 400: invalid durable notification payload",
            )
            continue
        if await db.claim_notification_delivery(
            int(row["notification_id"]), channel="telegram", stale_before=stale_before,
        ):
            _schedule(_deliver(int(row["notification_id"]), int(row["recipient_id"]), text))
            scheduled += 1
    return scheduled


async def drain() -> None:
    if not _tasks:
        return
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
