"""Защита лимита kinopoisk.dev (~200 запросов/сутки на free-тарифе).

Два механизма:
  1. Дневной бюджет реальных HTTP-вызовов Kinopoisk — жёсткий предохранитель под
     ~800/сутки (4 ключа × 200). Хранится в Postgres (database.try_spend_search_budget) —
     общий на все Fly-инстансы, атомарный инкремент без гонок при масштабировании.
  2. Per-user throttle — один пользователь не выжжет общую квоту. Остаётся
     in-memory (per-instance): при 2+ машинах даёт мягкую деградацию (эффективный
     лимит на юзера кратно числу машин), но это не проблема — жёсткий бюджет (п.1)
     всё равно останавливает суммарный расход вне зависимости от throttle.
"""
import os
import time
from collections import defaultdict, deque

import database as db
from config import KINOPOISK_TOKENS

# Бюджет HTTP-вызовов Kinopoisk в сутки. Если DAILY_SEARCH_BUDGET не задан явно —
# auto-обчислюється від кількості kinopoisk-токенів: 180 запитів/добу на токен
# (запас під ~200 free-ліміту kinopoisk.dev). 4 токени → 720/добу, не 2000.
# Так бюджет завжди відповідає реальній сумарній квоті джерел, навіть після
# додавання/видалення токенів — пошук не «ламається» від вичерпання квоти щоранку.
_env_budget = os.getenv("DAILY_SEARCH_BUDGET")
if _env_budget:
    DAILY_SEARCH_BUDGET: int = int(_env_budget)
else:
    _KP_DAILY_PER_TOKEN = int(os.getenv("KP_DAILY_PER_TOKEN", "180"))
    DAILY_SEARCH_BUDGET: int = max(1, len(KINOPOISK_TOKENS)) * _KP_DAILY_PER_TOKEN

# Per-user throttle: не более USER_MAX запросов за USER_WINDOW секунд.
USER_MAX: int = int(os.getenv("USER_SEARCH_MAX", "20"))
USER_WINDOW: int = int(os.getenv("USER_SEARCH_WINDOW", "60"))

# Public /img cannot carry Telegram initData because browsers do not attach the
# custom auth header to an <img>. Keep a separate, deliberately generous
# per-client guard for cache misses, in addition to the global fetch semaphore
# in main.py. It protects the application from becoming an open bandwidth proxy.
IMAGE_PROXY_MAX: int = int(os.getenv("IMAGE_PROXY_MAX", "120"))
IMAGE_PROXY_WINDOW: int = int(os.getenv("IMAGE_PROXY_WINDOW", "60"))
REVIEW_WRITE_MAX: int = int(os.getenv("REVIEW_WRITE_MAX", "12"))
REVIEW_WRITE_WINDOW: int = int(os.getenv("REVIEW_WRITE_WINDOW", "60"))
_MAX_TRACKED_KEYS: int = max(1_000, int(os.getenv("RATE_LIMIT_MAX_TRACKED_KEYS", "20_000")))

class _SlidingWindow:
    """One sliding-window counter.  Extracted because the same loop existed in
    three copies and a fourth was about to appear: a fix applied to one of them
    silently missed the others.

    The instance owns only the state.  Limits are passed in on every call and
    stay module-level constants, so an operator (or a test) can retune them at
    runtime without rebuilding the counter.
    """

    def __init__(self, max_tracked: int = _MAX_TRACKED_KEYS):
        self._max_tracked = max_tracked
        self._hits: dict[object, deque] = defaultdict(deque)
        self._calls = 0

    def _sweep(self, now: float, window: float) -> None:
        for key in list(self._hits):
            dq = self._hits[key]
            while dq and now - dq[0] > window:
                dq.popleft()
            if not dq:
                del self._hits[key]

    def allow(self, key: object, max_hits: int, window: float) -> bool:
        now = time.monotonic()
        self._calls += 1
        # Public traffic must not be able to grow this dict without bound.
        if self._calls % 500 == 0 or len(self._hits) >= self._max_tracked:
            self._sweep(now, window)
        dq = self._hits[key]
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= max_hits:
            return False
        dq.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()
        self._calls = 0


# List and rating writes get their own budget: normal usage bursts here (batch
# adding a franchise) and must not eat into the search quota.
MUTATION_MAX: int = int(os.getenv("MUTATION_MAX", "60"))
MUTATION_WINDOW: int = int(os.getenv("MUTATION_WINDOW", "60"))
# Quiz endpoints INSERT a session row per call, so they are the cheapest way for
# one account to write to Postgres in a loop.
QUIZ_MAX: int = int(os.getenv("QUIZ_MAX", "40"))
QUIZ_WINDOW: int = int(os.getenv("QUIZ_WINDOW", "60"))

_search_window = _SlidingWindow()
_image_window = _SlidingWindow()
_review_window = _SlidingWindow()
_mutation_window = _SlidingWindow()
_quiz_window = _SlidingWindow()


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


async def try_spend_search() -> bool:
    """True — можно сделать один HTTP-вызов Kinopoisk (единица списана из общего бюджета).
    False — дневной бюджет исчерпан, внешний вызов делать нельзя."""
    return await db.try_spend_search_budget(_today(), DAILY_SEARCH_BUDGET)


def allow_user(user_id: int) -> bool:
    """Search throttle: one account cannot burn the shared Kinopoisk quota."""
    return _search_window.allow(user_id, USER_MAX, USER_WINDOW)


def allow_image_proxy(client_key: str) -> bool:
    """Rate-limit cache misses of the unauthenticated image proxy.

    ``client_key`` is intentionally supplied by the HTTP boundary, where the
    deployment can decide whether a trusted proxy header is available. Keeping
    this in-memory is enough to cap one instance; the fetch semaphore provides
    the process-wide backstop when multiple clients are involved.
    """
    return _image_window.allow(client_key, IMAGE_PROXY_MAX, IMAGE_PROXY_WINDOW)


def allow_review_write(user_id: int) -> bool:
    """Separate review-mutation guard; review traffic never consumes search quota."""
    return _review_window.allow(user_id, REVIEW_WRITE_MAX, REVIEW_WRITE_WINDOW)


def allow_mutation(user_id: int) -> bool:
    """List and rating writes.  A separate budget from search: normal usage
    bursts here (batch-adding a franchise) without touching the search quota.
    """
    return _mutation_window.allow(user_id, MUTATION_MAX, MUTATION_WINDOW)


def allow_quiz(user_id: int) -> bool:
    """Quiz endpoints INSERT a session row per call, so they are the cheapest
    way for one account to write to Postgres in a loop.
    """
    return _quiz_window.allow(user_id, QUIZ_MAX, QUIZ_WINDOW)


def _reset_for_tests() -> None:
    """Tests only: drop the in-memory throttle state."""
    for window in (_search_window, _image_window, _review_window,
                   _mutation_window, _quiz_window):
        window.reset()
