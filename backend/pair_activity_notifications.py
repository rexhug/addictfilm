"""In-app notifications for activity inside one currently active pair."""
from __future__ import annotations

import logging

import database as db

logger = logging.getLogger(__name__)

PAIR_FILM_RATED = "pair.film.rated"
_TITLE_LIMIT = 120
_BODY_LIMIT = 500
_ACTION_LIMIT = 40
_NAME_LIMIT = 80
_FILM_TITLE_LIMIT = 160


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _partner_name(context: dict, language: str) -> str:
    name = _clean(
        context.get("actor_first_name") or context.get("actor_username"),
        _NAME_LIMIT,
    )
    if name:
        return name
    return "Партнёр" if language == "ru" else "Partner"


def format_partner_rating_notification(*, context: dict, rating: int) -> dict:
    """Build copy without exposing a score before the recipient has rated."""
    language = context.get("recipient_language")
    language = language if language in ("ru", "en") else "ru"
    name = _partner_name(context, language)
    film_title = _clean(context.get("film_title"), _FILM_TITLE_LIMIT)
    recipient_rating = context.get("recipient_rating")

    if language == "en":
        if recipient_rating is None:
            text = {
                "title": "Your partner rated a movie",
                "body": f'{name} rated “{film_title}”. Rate it too, then compare your scores.',
                "action_label": "Rate movie",
            }
        else:
            text = {
                "title": "New partner rating",
                "body": (
                    f'“{film_title}”: {name} rated it {int(rating)}/10. '
                    f"Your rating is {int(recipient_rating)}/10."
                ),
                "action_label": "Compare",
            }
    elif recipient_rating is None:
        text = {
            "title": "Партнёр оценил фильм",
            "body": (
                f"У {name} появилась оценка фильма «{film_title}». "
                "Оцени тоже — потом сравните."
            ),
            "action_label": "Оценить",
        }
    else:
        text = {
            "title": "Новая оценка партнёра",
            "body": (
                f"«{film_title}»: оценка {name} — {int(rating)}/10. "
                f"Твоя оценка — {int(recipient_rating)}/10."
            ),
            "action_label": "Сравнить",
        }

    return {
        "title": _clean(text["title"], _TITLE_LIMIT),
        "body": _clean(text["body"], _BODY_LIMIT),
        "action_label": _clean(text["action_label"], _ACTION_LIMIT),
    }


def rating_notification_key(
    *,
    pair_since: str,
    actor_id: int,
    recipient_id: int,
    film_id: int,
) -> str:
    """One event per pair session, direction and film."""
    return (
        f"pair-film-rated:{_clean(pair_since, 80)}:"
        f"{int(actor_id)}:{int(recipient_id)}:{int(film_id)}"
    )


async def notify_partner_film_rated(
    *,
    actor_id: int,
    film_id: int,
    rating: int,
    first_rating: bool,
) -> bool:
    """Materialize exactly one inbox row; never enqueue Telegram delivery."""
    if not first_rating:
        return False

    context = await db.get_partner_rating_notification_context(actor_id, film_id)
    if not context:
        return False

    recipient_id = int(context["partner_id"])
    if recipient_id == int(actor_id):
        return False

    text = format_partner_rating_notification(context=context, rating=rating)
    key = rating_notification_key(
        pair_since=str(context["pair_since"]),
        actor_id=actor_id,
        recipient_id=recipient_id,
        film_id=film_id,
    )
    notification_id, created = await db.create_notification_for_current_partner(
        actor_id=actor_id,
        recipient_id=recipient_id,
        pair_since=str(context["pair_since"]),
        event_type=PAIR_FILM_RATED,
        entity_id=str(film_id),
        payload={
            "title": text["title"],
            "body": text["body"],
            "action_label": text["action_label"],
            "event_type": PAIR_FILM_RATED,
            "film_id": int(film_id),
        },
        deep_link=f"movie:{int(film_id)}",
        idempotency_key=key,
    )
    return bool(notification_id is not None and created)
