"""Проверка Telegram Mini App initData (официальная схема HMAC).

Telegram подписывает данные пользователя ключом бота — подделать нельзя.
secret = HMAC_SHA256(key="WebAppData", msg=bot_token)
hash   = HMAC_SHA256(key=secret, msg=data_check_string)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age_sec: int = 86400) -> Optional[Dict[str, Any]]:
    """Возвращает dict пользователя ({'id':…, 'first_name':…}) или None, если подпись неверна.

    Алгоритм соответствует документации Telegram Web Apps: сначала вычисляется
    секрет как HMAC-SHA256(b"WebAppData", bot_token), затем считается HMAC
    по check_string и сравнивается в постоянное-времени.

    Возвращаемое значение — разобранный JSON поля `user` (только если это dict).
    """
    if not init_data or not bot_token:
        return None

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    given_hash = data.pop("hash", None)
    if not given_hash:
        return None

    # Собираем строку проверки в лексикографическом порядке ключей.
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))

    # Секрет: HMAC_SHA256(key=b"WebAppData", msg=bot_token)
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    # Безопасное сравнение хэшей
    if not hmac.compare_digest(calc, given_hash):
        return None

    # Защита от повторного использования старых initData. Требуем свежий auth_date.
    try:
        # auth_date может прийти как строка с десятичной частью у некоторых клиентов
        auth_date = int(float(data.get("auth_date", "0")))
    except (ValueError, TypeError):
        return None

    now = int(time.time())
    # Допуск на рассинхрон часов: +300 сек в будущее
    if max_age_sec and (auth_date == 0 or auth_date > now + 300 or now - auth_date > max_age_sec):
        return None

    try:
        user_field = data.get("user", "{}")
        user = json.loads(user_field)
        if not isinstance(user, dict):
            return None
        return user
    except json.JSONDecodeError:
        return None
