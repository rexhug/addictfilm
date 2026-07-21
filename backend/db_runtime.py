"""Единый асинхронный доступ к SQLite в разработке и PostgreSQL в продакшене.

Код прикладного слоя использует небольшой общий интерфейс ``execute``/
``fetchone``. Это позволяет хранить локальную тестовую базу SQLite, а публичный
сервис запускать на PostgreSQL без двух разных наборов бизнес-правил.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Sequence

import aiosqlite

try:  # Пакет нужен только когда задан PostgreSQL.
    import asyncpg
except ImportError:  # pragma: no cover - локальные тесты могут обходиться без него.
    asyncpg = None


_pool: Any | None = None
_pool_url: str | None = None


def uses_postgres(database_url: str) -> bool:
    """Определяет, нужно ли подключаться к PostgreSQL."""
    return database_url.startswith(("postgres://", "postgresql://"))


async def start(database_url: str) -> None:
    """Создаёт пул PostgreSQL один раз на процесс."""
    global _pool, _pool_url
    if not uses_postgres(database_url):
        return
    if _pool is not None:
        if _pool_url != database_url:
            raise RuntimeError("Нельзя менять DATABASE_URL во время работы приложения")
        return
    if asyncpg is None:
        raise RuntimeError("Для DATABASE_URL PostgreSQL установите зависимость asyncpg")
    _pool = await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=8,
        command_timeout=30,
        server_settings={"application_name": "movie-miniapp"},
    )
    _pool_url = database_url


async def close() -> None:
    """Закрывает пул PostgreSQL при штатной остановке приложения."""
    global _pool, _pool_url
    if _pool is not None:
        await _pool.close()
    _pool = None
    _pool_url = None


def _postgres_sql(sql: str) -> str:
    """Заменяет SQLite-плейсхолдеры на позиционные параметры PostgreSQL."""
    parts = sql.split("?")
    if len(parts) == 1:
        return sql
    return "".join(part if index == 0 else f"${index}{part}" for index, part in enumerate(parts))


def _rowcount(status: str) -> int:
    """Извлекает число затронутых строк из ответа asyncpg, например INSERT 0 1."""
    try:
        return int(status.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


class PostgresCursor:
    """Минимальный совместимый курсор для существующего слоя запросов."""

    def __init__(self, rows: Sequence[Any] = (), rowcount: int = 0):
        self._rows = list(rows)
        self._index = 0
        self.rowcount = rowcount
        self.lastrowid = None

    async def fetchone(self) -> Any | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    async def fetchall(self) -> list[Any]:
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows


class PostgresConnection:
    """Адаптер asyncpg под используемый в проекте интерфейс aiosqlite.

    Каждое соединение оборачивается в реальную asyncpg-транзакцию (см. connect()):
    несколько execute() внутри одного `async with connect()` — атомарны и невидимы
    другим соединениям до явного commit(). Это тот же контракт, что у SQLite/aiosqlite
    (где withoutCommit == потеря изменений) — код прикладного слоя не меняется.
    """

    def __init__(self, connection: Any, transaction: Any):
        self._connection = connection
        self._transaction = transaction
        self._done = False  # commit()/rollback() уже вызывали (или это сделает connect() сам)
        self.row_factory: Any = None

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> PostgresCursor:
        statement = sql.strip()
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("PRAGMA "):
            return PostgresCursor()

        statement = _postgres_sql(sql)
        query_type = statement.lstrip().split(None, 1)[0].upper()
        if query_type in {"SELECT", "WITH"} or " RETURNING " in statement.upper():
            rows = await self._connection.fetch(statement, *parameters)
            return PostgresCursor(rows, len(rows))
        status = await self._connection.execute(statement, *parameters)
        return PostgresCursor(rowcount=_rowcount(status))

    async def commit(self) -> None:
        if not self._done:
            await self._transaction.commit()
            self._done = True

    async def rollback(self) -> None:
        if not self._done:
            await self._transaction.rollback()
            self._done = True


@asynccontextmanager
async def connect(sqlite_path: str, database_url: str) -> AsyncIterator[Any]:
    """Возвращает соединение выбранной БД; SQLite остаётся локальным режимом.

    Postgres-ветка держит одну реальную транзакцию на весь блок `async with`:
    без явного commit() изменения теряются (откат) — как и у SQLite. Раньше
    каждый execute() коммитился в Postgres сам по себе (autocommit), из-за чего
    многошаговые операции (например, приём приглашения в пару) не были атомарны
    между несколькими Fly-машинами.
    """
    if uses_postgres(database_url):
        if _pool is None:
            await start(database_url)
        async with _pool.acquire() as connection:
            tr = connection.transaction()
            await tr.start()
            conn = PostgresConnection(connection, tr)
            try:
                yield conn
            except BaseException:
                if not conn._done:
                    await tr.rollback()
                    conn._done = True
                raise
            else:
                if not conn._done:  # забыли commit() — откатываем, а не тихо теряем контракт
                    await tr.rollback()
                    conn._done = True
        return

    # A short busy timeout prevents transient "database is locked" failures when
    # several API requests contend for the single SQLite writer. Foreign keys are
    # connection-scoped in SQLite, so they must be enabled for every connection.
    async with aiosqlite.connect(sqlite_path, timeout=5) as connection:
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("PRAGMA foreign_keys=ON")
        yield connection
