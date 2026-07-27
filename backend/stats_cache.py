"""Невеликий TTL-кеш для дорогих розрахунків статистики.

Кеш живе в пам'яті інстанса, тому не додає нової інфраструктури. Будь-яка
зміна списку або оцінки очищає його одразу; TTL лише захищає від повторних
відкриттів вкладки статистики між такими змінами.
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterable
from copy import deepcopy
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
    """Повний скид. Лишається для міграцій і тестів; у звичайному потоці
    користуйтеся invalidate_users — глобальний скид через оцінку одного
    користувача вимиває кеш усіх інших."""
    _cache.clear()


def invalidate_users(user_ids: Iterable[int]) -> int:
    """Прибрати лише записи, яких торкнулася зміна.

    Ключі: ("personal", user_id, year) та ("pair", user_id, partner_id, since) —
    тому достатньо звірити перші два-три елементи. Повертає кількість
    видалених записів (зручно для тестів і діагностики).
    """
    targets = {int(user_id) for user_id in user_ids if user_id is not None}
    if not targets:
        return 0
    doomed = [key for key in _cache
              if (len(key) > 1 and key[1] in targets)
              or (key and key[0] == "pair" and len(key) > 2 and key[2] in targets)]
    for key in doomed:
        _cache.pop(key, None)
    return len(doomed)
