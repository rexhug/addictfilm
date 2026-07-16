# Архитектура

Техническое описание Movie Mini App для разработчиков. Общее представление и
быстрый старт — в `README.md`; грабли и уроки — в `docs/LESSONS.md`; шаги
деплоя — в `docs/DEPLOY.md`. Этот документ не дублирует их, а связывает
модули backend в единую картину: кто кого вызывает и почему.

## 1. Общая схема

```
Telegram (кнопка меню бота)
   └─▶ frontend/            — Mini App: app.js + index.html + style.css
         │                    (Telegram WebApp SDK, тема из --tg-theme-*)
         └─▶ backend/main.py — FastAPI: раздаёт фронт + JSON API (20 роутов)
               │
               ├─ auth.py       — HMAC-проверка initData, upsert любого юзера
               ├─ database.py   — aiosqlite (WAL): users / films / user_films
               ├─ search.py     — оркестрация поиска: кэш → источники → кэш
               │     ├─ kinopoisk.py — основной источник (ротация токенов)
               │     ├─ omdb.py      — fallback + перевод запроса на английский
               │     └─ wikidata.py  — офиц. рус/укр названия (SPARQL)
               ├─ ratelimit.py  — дневной бюджет + per-user throttle
               ├─ ru.py         — русская локализация (плюрализация, даты, ★)
               └─ config.py     — .env → константы модулей выше
```

Mini App не умеет пушить сама — уведомления (если понадобятся) шлёт бот от
того же `BOT_TOKEN`, который открывает Mini App через `/setmenubutton`.

## 2. Backend — модуль за модулем

### `main.py` (335 строк) — точка входа

FastAPI-приложение: монтирует `frontend/` как статику и объявляет JSON API.
Роуты объявлены прямо на функциях (`route_path`/`route_method` в графе
кода — без отдельного слоя декораторов).

Полная таблица роутов:

| Метод | Путь | Обработчик | Назначение |
|---|---|---|---|
| GET | `/` | `index` | отдать `frontend/index.html` |
| GET | `/api/me` | `me` | профиль текущего юзера (+ статус пары) |
| GET | `/api/search?q=` | `api_search` | поиск фильмов (через `search.cached_search`) |
| GET | `/api/movies` | `movies` | список юзера: `status`, `sort`, `limit`, `offset` |
| POST | `/api/add` | `add` | добавить фильм в свой список |
| GET | `/api/movie/{film_id}` | `movie` | карточка фильма + community-рейтинг |
| DELETE | `/api/movie/{film_id}` | `delete` | убрать из своего списка (в каталоге остаётся) |
| POST | `/api/movie/{film_id}/rate` | `rate` | оценка 1–10 (авто-перевод в «просмотрено») |
| POST | `/api/movie/{film_id}/status` | `set_status` | смена статуса (хочу/смотрел) |
| POST | `/api/movie/{film_id}/comment` | `comment` | заметка к фильму |
| GET | `/api/browse` | `browse` | каталог: `sort` (popular/top), `genre`, пагинация |
| GET | `/api/genres` | `genres` | жанры каталога по убыванию частоты |
| GET | `/api/stats` | `stats` | личная статистика с графиками |
| GET | `/api/random` | `random_movie` | случайный фильм из «хочу посмотреть» |
| GET | `/api/partner` | `partner` | статус пары + бриф партнёра |
| POST | `/api/partner/invite` | `partner_invite` | создать/переиспользовать инвайт-токен |
| POST | `/api/partner/accept` | `partner_accept` | принять приглашение по токену |
| POST | `/api/partner/unpair` | `partner_unpair` | разорвать пару |
| GET | `/api/partner/stats` | `partner_stats` | совместная статистика пары |
| GET | `/img?u=` | `img_proxy` | прокси постеров (обход блокировки CDN) |

Вспомогательное: `current_user` — FastAPI-зависимость, дёргает
`auth.validate_init_data` из заголовка `X-Init-Data` и апсертит юзера;
`_img_sess` — ленивый aiohttp-session для прокси; `_invite_link` /
`_partner_brief` — сборка ответов для пары; `startup`/`shutdown` —
инициализация БД и закрытие HTTP-сессий источников.

`img_proxy` валидирует хост картинки по allowlist `_ALLOWED_IMG_HOSTS` перед
проксированием — так фронт всегда грузит постеры со своего домена, а не
напрямую с CDN kinopoisk/OMDb (которые Telegram/операторы иногда блокируют).

### `auth.py` (43 строки) — авторизация

Единственная публичная функция — `validate_init_data(init_data: str) -> dict | None`:
HMAC-SHA256 проверка подписи Telegram WebApp `initData` против `BOT_TOKEN`
(алгоритм из офиц. доков Telegram). Возвращает распарсенные данные юзера или
`None`, если подпись невалидна/просрочена. Никакого whitelist — любой
пользователь с корректной подписью проходит; регистрация — через
`database.upsert_user` в `main.current_user`.

### `database.py` (826 строк) — модель данных и вся бизнес-логика над ней

aiosqlite, WAL + `synchronous=NORMAL`. `init_db()` создаёт схему при старте;
`backup_db(keep=7)` — консистентный бэкап через `VACUUM INTO` с ротацией
(вызывать перед миграциями схемы — жёсткое правило проекта).

**Схема (мультитенантность):**
- `users` — любой Telegram-юзер, upsert без whitelist.
- `films` — общий каталог, dedup по `imdb_id` (`get_or_create_film`,
  `get_film`).
- `user_films` — per-user статус/оценка/комментарий, связь `user_id` + `film_id`.

**Список пользователя:** `add_to_list`, `remove_from_list` (убирает только из
`user_films`, каталог `films` не трогает), `set_rating` (оценка = неявное
«просмотрено» — см. `docs/LESSONS.md`, автоматически создаёт запись в
списке, если её не было), `set_status`, `set_comment`/`delete_comment`,
`get_user_film`, `get_user_films` (статусы `want_to_watch`/`watched`/`top` —
`top` сортирует просмотренные по личной оценке), `count_user_films`,
`get_random_want`, `get_unrated_watched` (для будущих напоминаний ботом).

**Каталог/discovery:** `browse_popular` (по числу добавивших), `browse_top`
(по средней оценке всех юзеров, честный порог `min_votes`), `browse_by_genre`,
`list_genres`, `community_rating` (среднее + число оценок по фильму).

**Личная статистика:** `get_user_stats` (KPI, гистограмма оценок, топ жанров/
актёров/режиссёров — сложность 27, самая длинная простая функция файла) и
`get_year_stats` (итоги конкретного года).

**Пара (Фаза E, опциональный слой):** `get_pair`/`get_partner`,
`create_invite` (переиспользует активный токен, если уже есть),
`get_pending_invite`, `accept_invite` (причины отказа: `invalid` / `self` /
`inviter_taken` / `already_paired` / `ok`), `unpair`, `sync_film_to_partner`
(синхрон «только новые» — идемпотентно добавляет фильм партнёру в «Хочу»,
существующие у него фильмы не трогает), `pair_period_stats` (совместная
статистика за «пар-период», сложность 44 — самая сложная функция в проекте).
Личные списки остаются приватными и без пары работают как обычно — пара
целиком опциональна.

**Постоянный кэш поиска (L2):** `search_cache_get`/`search_cache_put` —
SQLite-таблица под TTL-кэш результатов поиска (переживает рестарт процесса,
в отличие от L1 в `search.py`); `purge_search_cache` чистит протухшие записи.

### `search.py` (281 строка) — оркестрация поиска

`find_movies(query)` — источник истины по «сырому» поиску: если есть
`KINOPOISK_TOKEN` → `kinopoisk.search_movies`; иначе (или как расширение)
fallback на `omdb.search_movies` + `wikidata.get_titles_by_imdb`. Нормализует
результаты обоих источников в единый item-формат (`_kp_item`/`_omdb_item`).

Специальные правила, перенесённые из movie_bot и закреплённые тестами:
- `_is_sequel`/`_expand` — доиск сиквелов франшизы по английскому названию
  через OMDb (русские названия частей разные — иначе трилогии не собрать).
- `has_cyrillic`/`best_title` — название из Wikidata берётся только если оно
  кириллицей; латиница никогда не понижает уже хороший вариант.
- `_enrich_items` — параллельный дотяг постера/рейтинга/жанра из OMDb для
  fallback-результатов.

`fetch_details(src, ref)` — полные данные под `database.get_or_create_film`
(объединяет kinopoisk/OMDb/Wikidata в одну запись каталога).

**Многоуровневое кэширование + rate-limit** — сердце модуля,
`cached_search(query, user_id)`:
1. Нормализованный ключ (`_qnorm`) → L1 in-memory `_QCACHE` (TTL `_QTTL`,
   размер `_QMAX`, LRU-эвикция).
2. Промах L1 → L2 `database.search_cache_get` (TTL `_DB_TTL`, переживает
   рестарт).
3. Промах обоих → `ratelimit.allow_user` (per-user троттлинг) →
   `ratelimit.try_spend_search` (дневной бюджет) → только тогда реальный
   вызов `find_movies` → запись результата в оба кэша.

Возвращает `{items, cached, limited, throttled}` — фронт различает «пусто,
потому что не нашли» от «пусто, потому что упёрлись в лимит». Штрафуются
только реальные обращения к источнику, не кэш-хиты. `purge_expired` чистит
L2 периодически и на старте.

### `kinopoisk.py` (185 строк) — основной источник

`_request(path, params)` — GET с ротацией пула токенов (`KINOPOISK_TOKENS`):
при 401/402/403/429 (квота/доступ исчерпаны) пробует следующий ключ по кругу;
`None`, если ни один не ответил. `search_movies(query, limit=8)` и
`get_movie(kp_id)` (кэш в процессе) — основные точки входа, используются
`search.py`. `extract_credits`/`credits_by_imdb`/`ratings_by_imdb` — батчевые
запросы для бэкфила существующих записей каталога (режиссёры/актёры/рейтинги
по списку IMDb ID). `is_series`/`imdb_id_of` — хелперы нормализации.

### `omdb.py` (143 строки) — fallback-источник + перевод

`search_movies(query)` (сложность 18/51 когнитивной — самая сложная функция
модуля) — при неудаче на языке запроса пробует `translate_to_english`
(наивный словарь через `_translate_sync`) и повторяет запрос; при наличии
Wikidata-клиента также пробует `wikidata.search_movies` как ещё один
источник кандидатов. `_split_year` вытаскивает год из хвоста строки запроса
(`"Одержимость 2025"` → `("Одержимость", "2025")`). `get_movie(imdb_id)` —
кэш в процессе, как в `kinopoisk.py`.

### `wikidata.py` (143 строки) — официальные рус./укр. названия

`get_titles_by_imdb(imdb_ids, lang="ru")` — один SPARQL-запрос на весь список
ID → `{imdb_id: title}`; при любой ошибке/таймауте мягкий откат на пустой
словарь (поиск не падает, просто не обогащается). `search_movies(query)` —
поиск фильмов по названию на укр/рус напрямую через Wikidata API, в формате,
совместимом с OMDb (`{"Title", "Year", "imdbID"}`) — самая сложная функция
модуля (18/43), используется как ещё один fallback внутри `omdb.search_movies`.

### `ratelimit.py` (83 строки) — защита бюджета kinopoisk.dev

Два независимых механизма:
- **Дневной бюджет** (`try_spend_search`/`search_budget_left`) — глобальный
  счётчик внешних вызовов за день (`DAILY_SEARCH_BUDGET`), сбрасывается по
  дате (`_today`).
- **Per-user троттлинг** (`allow_user`) — скользящее окно
  (`USER_SEARCH_MAX` за `USER_SEARCH_WINDOW` секунд) на юзера, с периодической
  очисткой неактивных (`_sweep` — иначе состояние растёт без границ на
  публике).

Оба вызываются из `search.cached_search`, но **только на промахе кэша** —
кэш-хиты не тратят ни бюджет, ни лимит юзера.

### `ru.py` (90 строк) — русская локализация

Чистые хелперы без внешних зависимостей: `translate_genres` (англ. жанры →
русские), `plural_ru(n, one, few, many)` (1 фильм / 2 фильма / 5 фильмов),
`human_date` (ISO → «29 июня», год добавляется только если не текущий),
`compact_votes` (1074757 → «1.1M»), `stars` (оценка 1–10 → ★★★★☆),
`progress_bar` (процент → ▰▰▰▱▱), `esc` (HTML-экранирование).

### `config.py` (16 строк) — конфигурация

Читает `.env` через `python-dotenv` и экспортирует константы, которые
разбирают остальные модули: `BOT_TOKEN`, `BOT_USERNAME`, `KINOPOISK_TOKEN(S)`,
`OMDB_API_KEY`, `DB_PATH`, `DAILY_SEARCH_BUDGET`, `SEARCH_CACHE_TTL_SEC`,
`USER_SEARCH_MAX`, `USER_SEARCH_WINDOW`, `MIN_COMMUNITY_VOTES`.

## 3. Frontend (`frontend/`)

Ванильный JS без сборки — `app.js` (545 строк), `index.html`, `style.css`.
Тема берётся из CSS-переменных Telegram (`--tg-theme-*`), поддержка
light/dark. Ключевые узлы по фан-ину (из графа):

- `api(...)` — обёртка над `fetch` с заголовком `X-Init-Data`.
- `t(...)` — переводчик i18n (RU/EN).
- `esc(...)` — HTML-экранирование при рендере.
- `showHome`, `setActiveTab`, `emptyState`, `showGenre`, `showSearch` —
  навигация по вкладкам (Каталог / Мой список / Статистика / Пара).

## 4. Деплой

`Dockerfile` — `python:3.12-slim`, копирует `backend/` + `frontend/` +
`scripts/`, `DB_PATH=/data/movies.db` по умолчанию, запуск из `backend/`:
`uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}` (совместимо с
Fly.io/Railway, которые сами прокидывают `PORT`). `fly.toml` — конфигурация
Fly.io с постоянным томом под SQLite. Пошаговая инструкция — `docs/DEPLOY.md`.

Локальная разработка — без Docker: `uvicorn` напрямую + `cloudflared tunnel`
для публичного HTTPS, которого требует Telegram (см. `README.md`).

## 5. Прочее

- `scripts/import_movie_bot.py` — одноразовый импорт данных из legacy
  SQLite-базы бота `movie_bot` (не путать с текущим проектом — тот бот живёт
  отдельно, `com.moviebot` в launchd, не трогать).
- Полный перечень переменных окружения — `.env.example`; актуальный список
  подтверждён графом кода: `BOT_TOKEN`, `BOT_USERNAME`, `DAILY_SEARCH_BUDGET`,
  `DB_PATH`, `KINOPOISK_TOKEN`, `MIN_COMMUNITY_VOTES`, `OMDB_API_KEY`,
  `SEARCH_CACHE_TTL_SEC`, `USER_SEARCH_MAX`, `USER_SEARCH_WINDOW`.
- Статус фаз, роадмап — `README.md`; грабли (kinopoisk-хост, Wikidata 403,
  правило кириллицы, WAL, честные ничьи) — `docs/LESSONS.md`.
