"""Единое безопасное сопоставление текстовых маркеров.

Подстрочный поиск (`needle in text`) в этом проекте уже дважды приводил к
неверным рекомендациям: «месть» находилась во «вместе», «семь» — в «восемь», а
«черн» делало «Чернику» чёрной комедией. Маркеры здесь намеренно хранятся как
ПРЕФИКСЫ слов (русская морфология: «друж» → «дружба», «дружить»), поэтому
совпадение проверяется от начала слова, а не в любом его месте.

Модуль один на весь код: три почти одинаковых регулярных выражения в разных
файлах гарантированно разъезжаются, и разъехавшийся становится источником
следующего такого же дефекта.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache


def normalize_text(value: object) -> str:
    """Приводим к сравнимому виду, сохраняя дефис внутри слов.

    Дефис не выбрасываем: «feel-good» и «coming of age» — многословные маркеры,
    и разрыв их на части сделал бы совпадение невозможным.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^\w\s-]", " ", text)
    return " ".join(text.split())


@lru_cache(maxsize=4096)
def prefix_pattern(needle: str) -> re.Pattern[str]:
    """Маркер, привязанный к началу слова. Кэш — потому что маркеров десятки,
    а вызовов на каждый фильм сотни."""
    normalized = normalize_text(needle)
    return re.compile(rf"(?<!\w){re.escape(normalized)}", re.IGNORECASE)


def matches_any_prefix(text: object, needles: Iterable[str]) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(prefix_pattern(str(needle)).search(normalized) for needle in needles)


def matched_keys(text: object, groups: dict[str, tuple[str, ...]]) -> frozenset[str]:
    """Все ключи, чьи маркеры встретились в тексте."""
    normalized = normalize_text(text)
    if not normalized:
        return frozenset()
    return frozenset(key for key, needles in groups.items()
                     if any(prefix_pattern(str(needle)).search(normalized) for needle in needles))
