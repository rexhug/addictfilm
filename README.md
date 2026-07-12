# 🎬 Movie Mini App

**Публичный Telegram Mini App** для поиска, просмотра и оценивания фильмов
(модель Кинопоиск/Letterboxd, single-user). Любой пользователь Telegram
регистрируется при первом входе; у каждого свой список и оценки. Каталог
фильмов общий, community-рейтинг = средняя оценка всех пользователей.

Наследник movie_bot: перенесены **все проверенные логики** источников/поиска,
но данные разведены по пользователям (мультитенантность).

## Архитектура

```
Telegram (кнопка меню бота)
   └─▶ frontend/  — Mini App (HTML/JS, Telegram WebApp SDK, тема из Telegram)
         └─▶ backend/ — FastAPI: раздаёт фронт + JSON API
               ├─ auth.py      — проверка initData (HMAC), регистрируем любого юзера
               ├─ search.py    — поиск: kinopoisk.dev → fallback OMDb+Wikidata
               ├─ database.py  — SQLite (WAL), мультитенантность:
               │                  users / films (общий каталог) / user_films (per-user)
               └─ kinopoisk.py / omdb.py / wikidata.py — клиенты источников
```

Mini App пушить не умеет — уведомления (напоминания оценить и т.п.) при желании
шлёт **бот** от того же @BotFather; его токен и открывает Mini App.

## Быстрый старт

1. **Новый бот**: @BotFather → `/newbot` → получить `BOT_TOKEN`.
2. **Токен Кинопоиска**: написать @kinopoiskdev_bot в Telegram (без email!) → `KINOPOISK_TOKEN`.
3. `cp .env.example .env` и заполнить.
4. Установка и запуск:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cd backend && ../.venv/bin/uvicorn main:app --port 8077
   ```
5. **Туннель** (Telegram требует публичный HTTPS):
   ```bash
   brew install cloudflared
   cloudflared tunnel --url http://localhost:8077   # даст https://xxx.trycloudflare.com
   ```
6. @BotFather → `/setmenubutton` → вставить URL туннеля.
   Открыть бота → кнопка меню → Mini App работает.

Для постоянного URL: Cloudflare Tunnel со своим доменом (см. docs/LESSONS.md).

## Что уже перенесено из бота (проверено в бою)

- **Поиск**: kinopoisk.dev первым (рус. названия/постеры/рейтинги КП+IMDb/жанры/актёры
  одним запросом) → fallback OMDb + Wikidata (официальные рус. названия по SPARQL).
- **Франшизы**: «Мальчишник в Вегасе» находит всю трилогию (доиск сиквелов
  по английскому названию через OMDb, консервативный фильтр `_is_sequel`).
- **Правило кириллицы** (`best_title`): название из Wikidata берём только если оно
  кириллицей — латиница не понижает хороший вариант.
- **Схема БД** (мультитенантность): `users` / `films` (общий каталог, dedup по
  imdb_id) / `user_films` (per-user статус, оценка, коммент); WAL +
  synchronous=NORMAL; бэкап `backup_db()` (VACUUM INTO, ротация 7).
- **Статистика** (личная, per-user): экранное время, средняя оценка, любимые
  жанры/актёры/режиссёры (ничьи честно!), итоги года. Community-рейтинг фильма —
  средняя оценка всех пользователей.
- **RU-хелперы** (`ru.py`): жанры на русском, plural_ru, человеческие даты,
  компактные голоса (1.1M), звёзды ★★★★☆.

## Роадмап (публичный продукт)

- [x] **Фаза A — мультитенантный фундамент**: схема users/films/user_films,
      авторизация-upsert без whitelist, per-user эндпоинты, community-рейтинг,
      личная статистика. Проверено (24 e2e-теста, живой поиск kinopoisk).
- [x] **Фаза B — поиск + кэш-каталог**: cache-first (детали фильма из каталога
      films; кэш поисковых запросов), дневной бюджет внешних вызовов + per-user
      throttle под лимит kinopoisk (`backend/ratelimit.py`). Проверено (13+6 тестов).
- [x] **Фаза C — discovery**: публичный каталог (`/api/browse`: популярное /
      топ спильноты / по жанрам, `/api/genres`), вкладка «🌐 Каталог» с чипсами
      жанров, страница фильма с community-скором и «Хочу/Смотрел» из каталога.
      Проверено (13 e2e-тестов).
- [x] **Фаза D — персональная статистика** с графиками: KPI-плитки, гистограмма
      моих оценок (1–10), горизонтальные бары жанров/актёров/режиссёров, «Итоги
      года». Единый hue из темы Telegram, прямые подписи (проверено в light/dark).
- [x] **Деплой в облако**: Fly.io (2 машины, fra), health-check, keep-warm.
      Живёт на addict-film.fly.dev.
- [x] **Ops при росте**: SQLite→Postgres (Neon), кросс-инстансный дневной
      бюджет kinopoisk (атомарный UPSERT), Sentry. Платный тариф kinopoisk
      не нужен — 4 ключа × 200 = 800/сутки хватает с большим запасом.
- [ ] Веб-логин вне Telegram (сейчас только Telegram initData).
- [ ] CI (GitHub Actions → Fly deploy) — не подключен, деплой руками.
- [ ] Бот-уведомления (напоминания оценить) — есть только БД-заготовка
      (`get_unrated_watched`), сам бот не написан.

Все грабли и уроки — в **docs/LESSONS.md** (обязательно к прочтению).
