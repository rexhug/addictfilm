# Деплой в облако (стабильный URL для Mini App)

Публичный продукт не должен зависеть от Mac + временного тоннеля. Разворачиваем
контейнер в облаке с постоянным URL и томом для SQLite.

Файлы уже готовы: `Dockerfile`, `.dockerignore`, `fly.toml`. БД читает путь из
переменной `DB_PATH` (в облаке — `/data/movies.db` на постоянном томе).

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

# 3. Постоянный том для SQLite (регион как primary_region в fly.toml)
fly volumes create movies_data --region waw --size 1

# 4. Секреты (НЕ коммитим — задаём прямо в облаке)
fly secrets set \
  BOT_TOKEN=<токен бота Addict Film> \
  KINOPOISK_TOKEN=<токен kinopoisk.dev> \
  OMDB_API_KEY=<если есть, иначе пропусти>

# 5. Деплой
fly deploy
```

URL приложения: `https://<твоё-имя-приложения>.fly.dev`
→ вставить его в @BotFather → Menu Button (постоянный, больше не меняется).

Обновление после изменений в коде: `fly deploy`. Логи: `fly logs`.

> SQLite = одна машина. Не масштабировать горизонтально (`min_machines_running = 1`
> уже стоит). Данные переживают деплой, т.к. лежат на томе `movies_data`.

## Вариант B — Railway

1. Создать проект → «Deploy from GitHub repo» (или `railway up` из CLI).
2. Railway сам находит `Dockerfile`. Порт он передаёт через `$PORT` — уже учтено.
3. Добавить **Volume**, смонтировать в `/data`.
4. Переменные окружения: `DB_PATH=/data/movies.db`, `BOT_TOKEN`, `KINOPOISK_TOKEN`,
   `OMDB_API_KEY`.
5. Deploy → взять публичный URL из настроек → вставить в @BotFather.

## Локальная проверка контейнера (опционально)

```bash
docker build -t movie-miniapp .
docker run -p 8080:8080 --env-file .env -e DB_PATH=/tmp/movies.db movie-miniapp
# открыть http://localhost:8080/  (без initData вернёт 401 — это норма)
```
