"""Невеликий TTL-кеш для дорогих розрахунків статистики.

Кеш живе в пам'яті інстанса, тому не додає нової інфраструктури. Будь-яка
зміна списку або оцінки очищає його одразу; TTL лише захищає від повторних
відкриттів вкладки статистики між такими змінами.
"""
from __future__ import annotations

from copy import deepcopy
import os
import time
from typing import Any


_TTL_SECONDS = max(15, int(os.getenv("STATS_CACHE_TTL_SEC", "90")))
_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}


def get(key: tuple[Any, ...]) -> Any | None:
    entry = _cache.get(key)
    if entry is None or entry[0] <= time.monotonic():
        _cache.pop(key, None)
        return None
    return deepcopy(entry[1])


def put(key: tuple[Any, ...], value: Any) -> Any:
    _cache[key] = (time.monotonic() + _TTL_SECONDS, deepcopy(value))
    return deepcopy(value)


def clear() -> None:
    """Скинути кеш після зміни даних, що входять до статистики."""
    _cache.clear()
