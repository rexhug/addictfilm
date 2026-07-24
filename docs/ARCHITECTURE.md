# Архитектура Addict Film

Актуальное техническое описание публичного Telegram Mini App. Документ описывает
фактический код и production-модель: PostgreSQL/Neon в проде, SQLite — локальный
режим и fallback для разработки.

## Схема системы

```text
Telegram WebApp
  └─ frontend/ (vanilla JS, CSS, RU/EN)
       └─ FastAPI / backend/main.py
            ├─ auth.py: HMAC-проверка Telegram initData
            ├─ database.py: продуктовые запросы и схема
            ├─ db_runtime.py: SQLite / asyncpg adapter
            ├─ search.py: catalog-first поиск и кэши
            ├─ kinopoisk.py, omdb.py, wikidata.py: внешние источники
            ├─ ratelimit.py: лимиты внешних вызовов и image proxy
            ├─ stats_cache.py: короткий cache статистики
            └─ /img и /api/avatar: безопасные прокси изображений
```

`frontend/app.js` — единый клиентский модуль без сборщика. Он передаёт
`X-Init-Data` в каждый API-запрос, отменяет устаревшие detail/search запросы,
лениво загружает изображения и кратко кеширует home-rails в памяти вкладки.
`style.css` — фиксированная тёмная тема, не зависящая от цветовой темы Telegram.

## Данные

Основные таблицы:

- `users` — Telegram-профиль, роль редактора и `last_seen`;
- `films` — единый каталог, dedup по IMDb/Kinopoisk ID;
- `user_films` — личный статус, оценка, комментарий и даты;
- `film_genres` — производная индексируемая связь фильм ↔ жанр;
- `partners` и `partner_invites` — опциональная симметричная пара;
- `collections` и `collection_films` — публичные редакторские подборки;
- `search_cache` и `search_budget` — постоянный cache поиска и общий дневной
  бюджет внешних источников.

`films.genres`, актёры и режиссёры сохраняются как исходные поля каталога для
отображения; `film_genres` используется для точной быстрой фильтрации и списка
жанров. Миграция идемпотентна: при старте заполняет эту таблицу для legacy-фильмов.

## Горячие пути

### Авторизация и пользователь

`main.current_user` валидирует Telegram HMAC. `database.upsert_user` сразу
сохраняет изменения имени/username/avatar, но `last_seen` обновляет не чаще раза
в 15 минут — обычная навигация не создаёт write-нагрузку на Postgres.

### Каталог и поиск

Поиск сначала обращается к permanent catalog, затем к L1 process cache, затем к
L2 database cache; только cache miss может потратить лимит Kinopoisk/OMDb.
Обычные title queries используют prefix-путь по `idx_films_search_text`, а
infix/actor fallback сохраняет полный поиск. Это не заменяет полноценный FTS,
который понадобится лишь при существенно большем каталоге.

Discovery (`/api/browse`) считает community aggregates одним CTE по
`user_films`, а не несколькими correlated subquery для каждого фильма.
Фильтрация жанров идёт через `film_genres(genre, film_id)`.

### Личная и парная статистика

`/api/stats` и `/api/partner/stats` имеют 90-секундный bounded in-process TTL
cache. Любая мутация списка, оценки, комментария или пары очищает cache.
Личные годовые данные и общая статистика загружаются параллельно, если не зависят
друг от друга.

### Изображения

`/img` разрешает только явный allowlist CDN, проверяет redirect-цепочку, MIME и
лимит размера. Cache-hit и запись cache выполняются вне event loop; cache на
диске имеет ограничение по байтам/числу файлов и LRU trim. `/api/avatar/{id}`
выдаёт подписанный, ограниченный по паре proxy аватара партнёра.

## Postgres и SQLite

`db_runtime.connect` создаёт SQLite-соединение на локальной машине с WAL,
foreign keys и busy timeout. Для PostgreSQL используется asyncpg pool (1–8).
Read-only `SELECT` больше не открывает явную транзакцию; первая операция записи
лениво стартует transaction и сохраняет прежний контракт explicit `commit()`.
Это важно для атомарного accept invite и write-операций при нескольких Fly
инстансах.

## Производительность и наблюдаемость

- FastAPI GZip сжимает JS/CSS/API-ответы больше 512 байт;
- versioned `app.js`/`style.css` имеют immutable cache на год, HTML — `no-store`;
- middleware добавляет `Server-Timing`, логирует запросы от 750 мс и держит
  bounded latency window;
- `GET /api/admin/performance` доступен только с `ADMIN_TOKEN` и показывает
  среднее, p95 и 5xx по маршрутам текущего инстанса;
- Sentry принимает ошибки и небольшую configurable performance sample без PII.

Переменные оптимизации: `SENTRY_TRACES_SAMPLE_RATE`, `STATS_CACHE_TTL_SEC`,
`STATS_CACHE_MAX_ENTRIES`, `GENRES_CACHE_TTL_SEC`, `IMG_CACHE_*`,
`IMG_CACHE_TRIM_INTERVAL_SECONDS`, `REQUEST_METRICS_MAX_SAMPLES`,
`RATE_LIMIT_MAX_TRACKED_KEYS`.

## Проверки и деплой

GitHub Actions на каждом PR/main запускает compileall, `node --check`, unit tests,
контрактные PostgreSQL tests в PostgreSQL 16 service и `pip-audit`. Только после
успеха main автоматически деплоится в Fly.

Локально тесты запускаются из `backend/`:

```bash
../.venv/bin/python -m unittest discover -s tests -v
```

Production health: `GET /healthz` проверяет доступность активной базы данных,
а не только живость Python-процесса.
