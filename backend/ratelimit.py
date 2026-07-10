"""Защита лимита kinopoisk.dev (~200 запросов/сутки на free-тарифе).

Два механизма:
  1. Дневной бюджет ВНЕШНИХ поисковых вызовов — жёсткий предохранитель под 200/сутки.
  2. Per-user throttle — один пользователь не выжжет общую квоту.

Всё in-memory: сбрасывается при рестарте. Для масштаба (несколько инстансов)
заменить на Redis/БД — сейчас достаточно одного процесса. См. docs/LESSONS.md.
"""
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

# Бюджет внешних поисковых вызовов в сутки. С пулом токенов kinopoisk (ротация в
# kinopoisk.py суммирует лимиты, ~2k на ключ) запас большой; оставляем headroom
# под добавления/детали. Настраивается через .env (DAILY_SEARCH_BUDGET).
DAILY_SEARCH_BUDGET: int = int(os.getenv("DAILY_SEARCH_BUDGET", "2000"))

# Per-user throttle: не более USER_MAX запросов за USER_WINDOW секунд.
USER_MAX: int = int(os.getenv("USER_SEARCH_MAX", "20"))
USER_WINDOW: int = int(os.getenv("USER_SEARCH_WINDOW", "60"))

_day: str | None = None
_spent: int = 0
_hits: dict[int, deque] = defaultdict(deque)
_calls: int = 0  # счётчик обращений для периодической уборки _hits


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def try_spend_search() -> bool:
    """True — можно сделать внешний поисковый вызов (единица списана из бюджета).
    False — дневной бюджет исчерпан, внешний вызов делать нельзя."""
    global _day, _spent
    today = _today()
    if today != _day:  # новый день — сброс
        _day, _spent = today, 0
    if _spent >= DAILY_SEARCH_BUDGET:
        return False
    _spent += 1
    return True


def search_budget_left() -> int:
    if _today() != _day:
        return DAILY_SEARCH_BUDGET
    return max(0, DAILY_SEARCH_BUDGET - _spent)


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
    if _calls % 500 == 0:  # периодическая уборка «мёртвых» пользователей
        _sweep(now)
    dq = _hits[user_id]
    while dq and now - dq[0] > USER_WINDOW:
        dq.popleft()
    if len(dq) >= USER_MAX:
        return False
    dq.append(now)
    return True


def _reset_for_tests() -> None:
    """Только для тестов: обнулить состояние."""
    global _day, _spent, _calls
    _day, _spent, _calls = None, 0, 0
    _hits.clear()
