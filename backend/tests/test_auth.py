import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from auth import validate_init_data


BOT_TOKEN = "test:token"


def signed_init_data(*, auth_date: int, user: dict, tamper: bool = False) -> str:
    data = {"auth_date": str(auth_date), "query_id": "test-query", "user": json.dumps(user)}
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if tamper:
        data["query_id"] = "changed-after-signing"
    return urlencode(data)


class InitDataValidationTests(unittest.TestCase):
    def test_accepts_fresh_valid_telegram_data(self):
        user = {"id": 123, "first_name": "Denys"}
        actual = validate_init_data(signed_init_data(auth_date=int(time.time()), user=user), BOT_TOKEN)
        self.assertEqual(actual, user)

    def test_rejects_tampered_or_expired_data(self):
        user = {"id": 123}
        self.assertIsNone(validate_init_data(signed_init_data(auth_date=int(time.time()), user=user, tamper=True), BOT_TOKEN))
        self.assertIsNone(validate_init_data(signed_init_data(auth_date=1, user=user), BOT_TOKEN))
