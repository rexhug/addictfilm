import os

from dotenv import load_dotenv

# .env лежит в корне проекта (на уровень выше backend/).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


class ConfigurationError(RuntimeError):
    """Кривая настройка окружения. Сообщение НАЗЫВАЕТ переменную, но никогда не
    печатает её значение: половина этих переменных — секреты."""


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(low, min(float(raw), high))
    except ValueError:
        raise ConfigurationError(
            f"{name} должен быть числом от {low} до {high}") from None


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(low, min(int(raw), high))
    except ValueError:
        raise ConfigurationError(
            f"{name} должен быть целым числом от {low} до {high}") from None


def _flag(name: str, default: bool = False) -> bool:
    """Единая трактовка булевых переменных окружения.

    Раньше каждый модуль писал свой `in ("1","true","yes")` — расхождения в
    таком месте тихие и дорогие: флаг «выключен» в одном модуле и «включён» в
    соседнем выглядит как случайный баг, а не как опечатка в окружении.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _user_ids(name: str) -> set[int]:
    """Раньше опечатка в ADMIN_USER_IDS роняла импорт модуля голым ValueError:
    трассировка указывала в set-comprehension, а не на переменную окружения."""
    parsed: set[int] = set()
    for chunk in os.getenv(name, "").split(","):
        value = chunk.strip()
        if not value:
            continue
        try:
            parsed.add(int(value))
        except ValueError:
            raise ConfigurationError(
                f"{name} содержит нечисловой Telegram id — ожидается список чисел "
                f"через запятую") from None
    return parsed

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

# Невелика вибірка transaction traces дає реальні latency-показники в Sentry,
# не передаючи персональні дані. 0 повністю вимикає performance telemetry.
SENTRY_TRACES_SAMPLE_RATE: float = _bounded_float("SENTRY_TRACES_SAMPLE_RATE", 0.05, 0.0, 1.0)

# Токен обслуживания (бекфил постеров и т.п.). Пусто → админ-эндпоинты выключены.
ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "")

# Telegram user id, которым ВСЕГДА доступна in-app админка подборок (независимо от
# users.role в БД) — bootstrap без риска самозаблокироваться. Через запятую.
ADMIN_USER_IDS: set[int] = _user_ids("ADMIN_USER_IDS")

# Числовые настройки времени выполнения — с границами: опечатка в переменной не
# должна ни уронить процесс, ни выдать воркеру батч в миллион заданий.
ENRICHMENT_BATCH_SIZE: int = _bounded_int("ENRICHMENT_BATCH_SIZE", 5, 1, 100)
ENRICHMENT_IDLE_SLEEP: float = _bounded_float("ENRICHMENT_IDLE_SLEEP", 5.0, 0.5, 300.0)
ENRICHMENT_RECONCILE_INTERVAL: float = _bounded_float(
    "ENRICHMENT_RECONCILE_INTERVAL", 900.0, 60.0, 86400.0)

# ── Fanart.tv: горизонтальные кадры для полноэкранного экрана подбора ─────────
# Два ключа по документации Fanart: project key уходит заголовком `api-key`,
# личный — `client-key`. Оба необязательны по отдельности, но без хотя бы одного
# запросы не делаются вовсе. Значения живут ТОЛЬКО в окружении (Fly secrets).
FANART_PROJECT_KEY: str = os.getenv("FANART_PROJECT_KEY", "").strip()
FANART_CLIENT_KEY: str = os.getenv("FANART_CLIENT_KEY", "").strip()

# Внешнее обогащение кадрами. Выключено → воркер не ходит в Fanart вообще,
# уже сохранённые hero_* продолжают работать.
FANART_HERO_ENABLED: bool = _flag("FANART_HERO_ENABLED", False)

# Проверка УЖЕ СОХРАНЁННЫХ backdrop_url от kinopoisk. Отдельный канал, а не
# часть Fanart: он не ходит ни в какое стороннее API — только скачивает файл,
# ссылка на который уже лежит в каталоге. Именно поэтому он продолжает работать,
# когда Fanart недоступен, и наоборот.
KINOPOISK_HERO_ENABLED: bool = _flag("KINOPOISK_HERO_ENABLED", False)

# Полноэкранные экраны «Случайный из Хочу» и «Умный случайный». Выключено →
# работает прежний рендер карточки. Флаг НЕ связан с движком подбора: это
# только представление.
FULLSCREEN_SINGLE_PICK_ENABLED: bool = _flag("FULLSCREEN_SINGLE_PICK_ENABLED", False)

# Как часто воркер проверяет, кому пора обновить кадр.
HERO_REFRESH_INTERVAL: float = _bounded_float("HERO_REFRESH_INTERVAL", 900.0, 60.0, 86400.0)
HERO_REFRESH_BATCH: int = _bounded_int("HERO_REFRESH_BATCH", 20, 1, 200)
# Fanart просит не бомбить сервис: три одновременных запроса — потолок.
HERO_REFRESH_CONCURRENCY: int = _bounded_int("HERO_REFRESH_CONCURRENCY", 2, 1, 3)
