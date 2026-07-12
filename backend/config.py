import os
from dotenv import load_dotenv

# .env лежит в корне проекта (на уровень выше backend/).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Публичный Mini App: белого списка пользователей НЕТ — регистрируется любой,
# кто открыл приложение (авторизация по подписи initData, см. auth.py).
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# Пул токенов kinopoisk.dev: несколько ключей через запятую в KINOPOISK_TOKEN.
# Ротация в kinopoisk.py суммирует их суточные лимиты и даёт устойчивость к 403/квоте.
KINOPOISK_TOKENS: list[str] = [t.strip() for t in os.getenv("KINOPOISK_TOKEN", "").split(",") if t.strip()]
KINOPOISK_TOKEN: str = KINOPOISK_TOKENS[0] if KINOPOISK_TOKENS else ""  # для проверок «есть ли ключ»

OMDB_API_KEY: str = os.getenv("OMDB_API_KEY", "")

# Postgres в проде (Neon), SQLite локально — см. db_runtime.py. Пусто → SQLite.
DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()

# Мониторинг ошибок (Sentry, провижн через `fly ext sentry`). Пусто → выключен (локально).
SENTRY_DSN: str = os.getenv("SENTRY_DSN", "").strip()

# Токен обслуживания (бекфил постеров и т.п.). Пусто → админ-эндпоинты выключены.
ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "")

# Telegram user id, которым ВСЕГДА доступна in-app админка подборок (независимо от
# users.role в БД) — bootstrap без риска самозаблокироваться. Через запятую.
ADMIN_USER_IDS: set[int] = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}
