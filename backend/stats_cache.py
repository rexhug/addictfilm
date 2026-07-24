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
_MAX_ENTRIES = max(100, int(os.getenv("STATS_CACHE_MAX_ENTRIES", "2000")))
_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}


def get(key: tuple[Any, ...]) -> Any | None:
    entry = _cache.get(key)
    if entry is None or entry[0] <= time.monotonic():
        _cache.pop(key, None)
        return None
    return deepcopy(entry[1])


def put(key: tuple[Any, ...], value: Any) -> Any:
    now = time.monotonic()
    # A public app can have many users who each open stats once. Expired entries
    # otherwise remain forever because they are only removed on a matching get.
    if len(_cache) >= _MAX_ENTRIES:
        expired = [cache_key for cache_key, (expires, _) in _cache.items() if expires <= now]
        for cache_key in expired:
            _cache.pop(cache_key, None)
    if len(_cache) >= _MAX_ENTRIES:
        for cache_key, _ in sorted(_cache.items(), key=lambda item: item[1][0])[:max(1, _MAX_ENTRIES // 10)]:
            _cache.pop(cache_key, None)
    _cache[key] = (now + _TTL_SECONDS, deepcopy(value))
    return deepcopy(value)


def clear() -> None:
    """Скинути кеш після зміни даних, що входять до статистики."""
    _cache.clear()
