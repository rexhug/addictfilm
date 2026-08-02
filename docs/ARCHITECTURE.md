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
- `user_films` — личный статус, оценка, текущий текст отзыва/legacy-заметки и даты;
- `review_identities` и `review_reports` — стабильный ID публичного отзыва,
  keyset-пагинация и уникальные жалобы без дублирования текста;
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

### Публичные отзывы

Текст и оценка остаются в единственной записи `user_films` на пользователя и
фильм. `comment_status` явно разделяет `published`, `hidden`, `deleted` и
`private_legacy`; миграция никогда не публикует исторические личные заметки.
Опубликовать legacy-текст может только сам пользователь отдельным действием.

`GET /api/movie/{film_id}/reviews` возвращает bounded keyset-страницы и
поддерживает сортировки `newest`, `highest`, `lowest`. Отзыв активного
взаимного партнёра возвращается отдельно и не дублируется в общей ленте.
Создание/редактирование использует один стабильный review ID, ограничено
500 символами и отдельным rate limit. Пользователь может удалить свой отзыв,
пожаловаться на чужой, а редактор — скрыть опубликованный отзыв; скрытые и
удалённые тексты в публичный API не попадают.

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

## Known limitation: pool occupancy during upstream calls

После первого обращения к базе request-scoped connection остаётся закреплённым
за запросом до его завершения. Поэтому endpoints, которые сначала читают базу,
а затем ждут Kinopoisk, OMDb, Wikidata или image proxy, могут удерживать слот
пула во время внешнего I/O. Lazy acquisition не занимает слот для запросов,
которые базу не трогают; разделение внешних вызовов и database transaction
останется отдельной задачей при дальнейшем росте нагрузки.

### Миграции

`schema_migrations` хранит журнал успешно применённых изменений схемы. Старые
колонки добавляются отдельной идемпотентной миграцией; ожидаемое «колонка уже
есть» безопасно пропускается, но ошибки сети, прав, синтаксиса или диска больше
не скрываются. Новое изменение схемы добавляется как отдельный шаг миграции и
отмечается в журнале только после успешного выполнения.

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

GitHub Actions на каждом PR/main запускает Ruff, ESLint, compileall,
`node --check`, unit tests, контрактные PostgreSQL tests в PostgreSQL 16 service,
`pip-audit` и Playwright-проверки мобильной статистики (390px: личный и парный
режимы, отсутствие горизонтального скролла и перекрытия нижней навигацией).
Только после успеха main автоматически деплоится в Fly.

Локально тесты запускаются из `backend/`:

```bash
../.venv/bin/python -m unittest discover -s tests -v
```

Из корня репозитория:

```bash
ruff check backend scripts
npm ci
npm run lint
npx playwright install chromium
npm run test:e2e
```

Production health: `GET /healthz` проверяет доступность активной базы данных,
а не только живость Python-процесса.
