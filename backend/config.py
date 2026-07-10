import os
from dotenv import load_dotenv

# .env лежит в корне проекта (на уровень выше backend/).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
KINOPOISK_TOKEN: str = os.getenv("KINOPOISK_TOKEN", "")
OMDB_API_KEY: str = os.getenv("OMDB_API_KEY", "")
USER1_ID: int = int(os.getenv("USER1_ID", "0"))
USER2_ID: int = int(os.getenv("USER2_ID", "0"))

ALLOWED_USERS: set[int] = {uid for uid in (USER1_ID, USER2_ID) if uid != 0}

USER_LABELS: dict[int, str] = {
    uid: label
    for uid, label in ((USER1_ID, "Денис"), (USER2_ID, "Котятко"))
    if uid != 0
}


def partner_of(user_id: int) -> int:
    return USER2_ID if user_id == USER1_ID else USER1_ID
