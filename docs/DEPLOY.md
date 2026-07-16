# Деплой в облако (стабильный URL для Mini App)

Публичный продукт не должен зависеть от Mac + временного тоннеля. Продакшн
развёрнут на Fly.io, а основная БД — Neon PostgreSQL через секрет `DATABASE_URL`.
SQLite остаётся только для локальной разработки; legacy-том `/data` не является
источником production-данных.

Файлы уже готовы: `Dockerfile`, `.dockerignore`, `fly.toml`. На Fly секрет
`DATABASE_URL` обязателен; без него приложение переключится на локальный SQLite.

## Вариант A — Fly.io (рекомендуется)

Одноразовые шаги (нужен аккаунт Fly и вход в браузере — это делаешь ты):

```bash
# 1. Установить flyctl и войти (откроется браузер)
brew install flyctl
fly auth login

# 2. Создать приложение (имя должно быть глобально уникальным).
#    Замени addict-film в fly.toml на своё имя ПЕРЕД этим, либо:
fly apps create <твоё-имя-приложения>
#    → и впиши это же имя в поле app в fly.toml

# 3. Секреты (НЕ коммитим — задаём прямо в облаке)
fly secrets set \
  DATABASE_URL=<postgresql://...> \
  BOT_TOKEN=<токен бота Addict Film> \
  KINOPOISK_TOKEN=<токен kinopoisk.dev> \
  OMDB_API_KEY=<если есть, иначе пропусти> \
  ADMIN_USER_IDS=<telegram_id_админа>

# 4. Деплой
fly deploy
```

URL приложения: `https://<твоё-имя-приложения>.fly.dev`
→ вставить его в @BotFather → Menu Button (постоянный, больше не меняется).

Пуш у `main` автоматически запускает GitHub Actions: спершу smoke-тести, потім
деплой на Fly. `fly deploy` лишається для аварійного ручного релізу. Логи:
`fly logs`.

> Neon/Postgres дозволяє кілька інстансів Fly. Дані переживають деплой у Neon;
> перед зміною схеми перевіряй міграцію на копії БД.

## Вариант B — Railway

1. Создать проект → «Deploy from GitHub repo» (или `railway up` из CLI).
2. Railway сам находит `Dockerfile`. Порт он передаёт через `$PORT` — уже учтено.
3. Додати керований PostgreSQL або Neon і передати його URL як `DATABASE_URL`.
4. Переменные окружения: `DATABASE_URL`, `BOT_TOKEN`, `KINOPOISK_TOKEN`,
   `OMDB_API_KEY`, `ADMIN_USER_IDS`.
5. Deploy → взять публичный URL из настроек → вставить в @BotFather.

## Локальная проверка контейнера (опционально)

```bash
docker build -t movie-miniapp .
docker run -p 8080:8080 --env-file .env -e DB_PATH=/tmp/movies.db movie-miniapp
# открыть http://localhost:8080/  (без initData вернёт 401 — это норма)
```
