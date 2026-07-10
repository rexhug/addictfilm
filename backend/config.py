import os
from dotenv import load_dotenv

# .env лежит в корне проекта (на уровень выше backend/).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "").lstrip("@")
KINOPOISK_TOKEN: str = os.getenv("KINOPOISK_TOKEN", "")
OMDB_API_KEY: str = os.getenv("OMDB_API_KEY", "")
DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()
