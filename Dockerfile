# Публичный Mini App: FastAPI раздаёт фронт и API; отдельный процесс — обогащение.
FROM python:3.12-slim

WORKDIR /app

# Unbuffered: Fly collects logs from stdout, and buffering hides the last lines
# of a crash — exactly the ones that explain it.
# No .pyc: the image runs read-only as appuser, so bytecode has nowhere to go.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

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
