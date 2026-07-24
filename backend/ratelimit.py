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
_MAX_TRACKED_KEYS: int = max(1_000, int(os.getenv("RATE_LIMIT_MAX_TRACKED_KEYS", "20_000")))

_hits: dict[int, deque] = defaultdict(deque)
_calls: int = 0  # счётчик обращений для периодической уборки _hits
_image_hits: dict[str, deque] = defaultdict(deque)
_image_calls: int = 0


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


async def try_spend_search() -> bool:
    """True — можно сделать один HTTP-вызов Kinopoisk (единица списана из общего бюджета).
    False — дневной бюджет исчерпан, внешний вызов делать нельзя."""
    return await db.try_spend_search_budget(_today(), DAILY_SEARCH_BUDGET)


def _sweep(now: float) -> None:
    """Убрать записи неактивных пользователей (иначе _hits растёт на публике)."""
    for uid in list(_hits):
        dq = _hits[uid]
        while dq and now - dq[0] > USER_WINDOW:
            dq.popleft()
        if not dq:
            del _hits[uid]


def allow_user(user_id: int) -> bool:
    """Скользящее окно: False, если пользователь превысил USER_MAX за USER_WINDOW сек."""
    global _calls
    now = time.monotonic()
    _calls += 1
    if _calls % 500 == 0 or len(_hits) >= _MAX_TRACKED_KEYS:  # не даём публичному трафику раздувать память
        _sweep(now)
    dq = _hits[user_id]
    while dq and now - dq[0] > USER_WINDOW:
        dq.popleft()
    if len(dq) >= USER_MAX:
        return False
    dq.append(now)
    return True


def allow_image_proxy(client_key: str) -> bool:
    """Rate-limit cache misses of the unauthenticated image proxy.

    ``client_key`` is intentionally supplied by the HTTP boundary, where the
    deployment can decide whether a trusted proxy header is available. Keeping
    this in-memory is enough to cap one instance; the fetch semaphore provides
    the process-wide backstop when multiple clients are involved.
    """
    global _image_calls
    now = time.monotonic()
    _image_calls += 1
    if _image_calls % 500 == 0 or len(_image_hits) >= _MAX_TRACKED_KEYS:
        for key in list(_image_hits):
            dq = _image_hits[key]
            while dq and now - dq[0] > IMAGE_PROXY_WINDOW:
                dq.popleft()
            if not dq:
                del _image_hits[key]
    dq = _image_hits[client_key]
    while dq and now - dq[0] > IMAGE_PROXY_WINDOW:
        dq.popleft()
    if len(dq) >= IMAGE_PROXY_MAX:
        return False
    dq.append(now)
    return True


def _reset_for_tests() -> None:
    """Только для тестов: обнулить in-memory состояние (per-user throttle)."""
    global _calls, _image_calls
    _calls = 0
    _hits.clear()
    _image_calls = 0
    _image_hits.clear()
