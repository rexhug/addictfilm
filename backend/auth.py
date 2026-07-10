"""Проверка Telegram Mini App initData (официальная схема HMAC).

Telegram подписывает данные пользователя ключом бота — подделать нельзя.
secret = HMAC_SHA256(key="WebAppData", msg=bot_token)
hash   = HMAC_SHA256(key=secret, msg=data_check_string)
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age_sec: int = 86400) -> dict | None:
    """Возвращает dict пользователя ({'id':…, 'first_name':…}) или None, если подпись неверна."""
    if not init_data or not bot_token:
        return None
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    given_hash = data.pop("hash", None)
    if not given_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, given_hash):
        return None

    # Защита от повторного использования старых initData.
    try:
        auth_date = int(data["auth_date"])
    except ValueError:
        return None
    except KeyError:
        return None
    now = time.time()
    if auth_date <= 0 or auth_date > now + 60:
        return None
    if max_age_sec and now - auth_date > max_age_sec:
        return None

    try:
        user = json.loads(data.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None
