"""In-process gate in front of upsert_user.

The UPSERT itself is already a no-op when nothing changed, but it still costs
BEGIN + statement + COMMIT against Neon on every single API request — and a
Mini App screen fires several. The database is not the right place to answer
"did anything change in the last five minutes"; the process already knows.

Per-instance and deliberately so: a miss costs one harmless UPSERT, and Fly
machines restarting simply re-warm. Nothing here is a source of truth.
"""
import time

TTL_SECONDS: float = 300.0
_MAX_ENTRIES: int = 50_000

# Key carries every field upsert_user would write, so a rename or a new avatar
# still writes through immediately instead of waiting out the TTL.
_seen: dict[tuple, float] = {}


def _key(user: dict) -> tuple:
    return (user.get("id"), user.get("first_name"), user.get("username"), user.get("photo_url"))


def should_persist(user: dict, now: float | None = None) -> bool:
    """True when upsert_user must actually run for this user right now."""
    moment = time.monotonic() if now is None else now
    key = _key(user)
    last = _seen.get(key)
    if last is not None and moment - last < TTL_SECONDS:
        return False
    if len(_seen) >= _MAX_ENTRIES:
        # Public app: drop everything stale rather than grow without bound.
        cutoff = moment - TTL_SECONDS
        for stale in [k for k, seen_at in _seen.items() if seen_at < cutoff]:
            del _seen[stale]
        if len(_seen) >= _MAX_ENTRIES:
            _seen.clear()
    _seen[key] = moment
    return True


def _reset_for_tests() -> None:
    _seen.clear()
