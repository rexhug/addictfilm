import os
from dotenv import load_dotenv

# .env лежит в корне проекта (на уровень выше backend/).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Публичный Mini App: белого списка пользователей НЕТ — регистрируется любой,
# кто открыл приложение (авторизация по подписи initData, см. auth.py).
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
KINOPOISK_TOKEN: str = os.getenv("KINOPOISK_TOKEN", "")
OMDB_API_KEY: str = os.getenv("OMDB_API_KEY", "")
