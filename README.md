# 🎬 Movie Mini App — скелет

Telegram Mini App для совместного трекинга фильмов (Денис + Котятко).
Наследник movie_bot: сюда перенесены **все проверенные логики** из бота,
но без чат-специфики. Это скелет — UI и фичи наращиваются поверх.

## Архитектура

```
Telegram (кнопка меню бота)
   └─▶ frontend/  — Mini App (HTML/JS, Telegram WebApp SDK, тема из Telegram)
         └─▶ backend/ — FastAPI: раздаёт фронт + JSON API
               ├─ auth.py      — проверка initData (HMAC), пускаем только двоих
               ├─ search.py    — поиск: kinopoisk.dev → fallback OMDb+Wikidata
               ├─ database.py  — SQLite (WAL): movies / ratings / comments + статистика
               └─ kinopoisk.py / omdb.py / wikidata.py — клиенты источников
```

Уведомления партнёру шлёт **бот** (mini app пушить не умеет) — новый бот
создаётся у @BotFather, его токен и открывает Mini App.

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
- **Схема БД**: movies (kp_rating, directors, actors, title_original…), ratings,
  comments; WAL + synchronous=NORMAL; бэкап `backup_db()` (VACUUM INTO, ротация 7).
- **Статистика**: экранное время, совместимость (%), точные совпадения вкусов,
  любимые актёры/режиссёры (ничьи честно!), итоги года, самый спорный.
- **RU-хелперы** (`ru.py`): жанры на русском, plural_ru, человеческие даты,
  компактные голоса (1.1M), звёзды ★★★★☆.

## Роадмап (предложение)

- [ ] Экран «Списки»: сетка постеров, фильтры Хотим/Просмотрено/Топ
- [ ] Карточка: постер, рейтинги, оценка тапом 1–10, комментарий, статус, удалить
- [ ] Поиск с живыми результатами
- [ ] Статистика с графиками (совместимость, жанры, актёры)
- [ ] Уведомления партнёру через бота (sendMessage при действиях в аппе)
- [ ] Постоянный домен + Cloudflare Tunnel

Все грабли и уроки — в **docs/LESSONS.md** (обязательно к прочтению).
