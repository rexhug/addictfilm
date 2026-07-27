# Публичный Mini App: FastAPI раздаёт фронт и API; отдельный процесс — обогащение.
FROM python:3.12-slim

WORKDIR /app

# Зависимости кэшируются, пока requirements.txt не менялся.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/

# Непривилегированный пользователь: процесс приложения не должен иметь права
# менять собственный код или систему. Фиксированный uid — чтобы права на томе
# Fly можно было выдать один раз и они пережили пересборку образа.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

# База в облаке — Postgres (DATABASE_URL). DB_PATH остаётся для локального
# SQLite-режима; том /data используется только под кэш картинок.
ENV DB_PATH=/data/movies.db
WORKDIR /app/backend
EXPOSE 8080

# $PORT задаёт хостинг (Railway); по умолчанию 8080 (Fly.io internal_port).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
