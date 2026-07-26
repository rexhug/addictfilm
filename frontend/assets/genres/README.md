# Жанровые фоны (пакет WebP)

Сюда кладётся кураторский пакет из 12 файлов (имена фиксированы маппингом
`GENRE_BACKDROPS` в `frontend/app.js`):

drama.webp, action.webp, comedy.webp, thriller.webp, adventure.webp,
scifi.webp, crime.webp, mystery.webp, fantasy.webp, horror.webp,
animation.webp, romance.webp

Рекомендации: ~688×430 (16/10, 2x), тёмные сдержанные кадры без текста,
< 60 KB каждый. Пакет установлен (2026-07-26). Флаг уже включён:
`GENRE_BACKDROPS_READY = true` в app.js — других правок не требуется
(битый/отсутствующий файл автоматически падает на tint-фолбэк).
