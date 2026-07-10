# Публичный Mini App: FastAPI + SQLite (том), раздаёт фронт и API.
FROM python:3.12-slim

WORKDIR /app

# Зависимости кэшируются, пока requirements.txt не менялся.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/

# SQLite живёт на постоянном томе (см. fly.toml [mounts] / переменную DB_PATH).
ENV DB_PATH=/data/movies.db
WORKDIR /app/backend
EXPOSE 8080

# $PORT задаёт хостинг (Railway); по умолчанию 8080 (Fly.io internal_port).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
