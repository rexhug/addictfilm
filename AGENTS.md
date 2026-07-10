# Movie Mini App — инструкции для агента

Telegram Mini App для совместного кино-трекинга пары (Денис + Котятко).
Наследник проекта `/Users/denyszapriahailo/movie_bot` (работающий бот) —
**тот проект НЕ трогать**, он живёт своей жизнью (launchd `com.moviebot`).

## Перед началом работы
1. Прочитай `README.md` (архитектура, быстрый старт).
2. Прочитай `docs/LESSONS.md` — там ВСЕ добытые грабли (kinopoisk-хост и
   selectFields-квирк, Wikidata 403/User-Agent, правило кириллицы, WAL, честные ничьи).
   Не наступать повторно.

## Жёсткие правила
- **Весь UI — на русском** (пользовательница — Кристина). Комментарии в коде тоже русские.
- **TMDb не предлагать**: у Дениса не приходит письмо верификации, ключ получить
  невозможно. Основной источник — kinopoisk.dev (токен через @kinopoiskdev_bot).
- Пользователей всегда двое (USER1_ID/USER2_ID из .env) — никакой мультитенантности.
- Данные бережём: перед миграциями схемы — бэкап (`database.backup_db`).
- Секреты только в `.env` (gitignored).

## Стек и запуск
- Backend: FastAPI + aiosqlite (WAL), из `backend/`: `../.venv/bin/uvicorn main:app --port 8077`
- Frontend: ванильный JS + Telegram WebApp SDK, тема из `--tg-theme-*`.
- Авторизация: initData HMAC (`backend/auth.py`), заголовок `X-Init-Data`.
- Публичный HTTPS для Telegram: `cloudflared tunnel --url http://localhost:8077`
  (прототип) или Cloudflare Tunnel с доменом (постоянно).
- venv уже создан: `.venv/` (fastapi, uvicorn, aiosqlite, aiohttp, python-dotenv).

## Состояние
Скелет готов и проверен (эндпоинты работают, фронт отдаётся, 401 без initData).
`.env` заполнен ЗАГЛУШКАМИ — нужен реальный BOT_TOKEN нового бота от @BotFather.
Роадмап — в README.md.
