"""Единый асинхронный доступ к SQLite в разработке и PostgreSQL в продакшене.

Код прикладного слоя использует небольшой общий интерфейс ``execute``/
``fetchone``. Это позволяет хранить локальную тестовую базу SQLite, а публичный
сервис запускать на PostgreSQL без двух разных наборов бизнес-правил.
"""
from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

try:  # Пакет нужен только когда задан PostgreSQL.
    import asyncpg
except ImportError:  # pragma: no cover - локальные тесты могут обходиться без него.
    asyncpg = None


_pool: Any | None = None
_pool_url: str | None = None
_CURRENT_CONNECTION: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "db_runtime_current_connection", default=None,
)


def current_connection() -> Any | None:
    """Return the request-scoped connection, if one is bound."""
    return _CURRENT_CONNECTION.get()


class _BorrowedConnection:
    """Proxy that shares a request connection without taking ownership."""

    def __init__(self, connection: Any):
        object.__setattr__(self, "_connection", connection)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    @property
    def row_factory(self) -> Any:
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._connection.row_factory = value

    async def commit(self) -> None:
        """Nested database helpers cannot commit the request transaction."""

    async def rollback(self) -> None:
        """Nested database helpers cannot roll back the request transaction."""

    async def begin_nested_write(self) -> Any | None:
        """Open a savepoint on the request owner, when the backend supports it."""
        return await self._connection.begin_nested_write()

    async def finish_nested(self, savepoint: Any, failed: bool) -> None:
        """Finish a nested transaction through the request owner's lock."""
        return await self._connection.finish_nested(savepoint, failed)

    async def release_if_idle(self) -> bool:
        """Release an idle request connection without ending the request scope."""
        return await self._connection.release_if_idle()


def _statement_is_read_only(sql: str) -> bool:
    """Return whether a statement can run without a write savepoint."""
    query_type = sql.strip().upper().split(None, 1)[0] if sql.strip() else ""
    return query_type in {"SELECT", "PRAGMA", "SHOW", "EXPLAIN", "VALUES"}


class _NestedBorrowedConnection:
    """Request-borrowed connection isolated by one lazy PostgreSQL savepoint.

    SQLite deliberately bypasses this proxy: its request connection keeps the
    existing behaviour and emits no SAVEPOINT SQL.  PostgreSQL opens a nested
    transaction only when this block performs its first write, so read-only
    helpers do not pay for a savepoint.
    """

    def __init__(self, connection: _BorrowedConnection):
        self._connection = connection
        self._savepoint: Any | None = None

    @property
    def row_factory(self) -> Any:
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._connection.row_factory = value

    async def _ensure_savepoint(self, sql: str) -> None:
        if _statement_is_read_only(sql) or self._savepoint is not None:
            return
        self._savepoint = await self._connection.begin_nested_write()

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        await self._ensure_savepoint(sql)
        return await self._connection.execute(sql, parameters)

    async def commit(self) -> None:
        """The context manager owns the savepoint; never commit the request."""

    async def rollback(self) -> None:
        """The context manager owns the savepoint; never roll back the request."""

    async def finish(self, failed: bool) -> None:
        if self._savepoint is None:
            return
        savepoint = self._savepoint
        self._savepoint = None
        await self._connection.finish_nested(savepoint, failed)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _LazyRequestConnection:
    """Lazily acquire the request-owned database connection on first use.

    The HTTP middleware binds this object before the handler runs, but cheap
    endpoints (static files, health checks that do not touch the database, and
    image proxy requests) never need to occupy a pool slot.  ``row_factory`` is
    intentionally buffered until acquisition so existing database helpers can
    keep assigning it before their first query.
    """

    def __init__(self, sqlite_path: str, database_url: str):
        self._sqlite_path = sqlite_path
        self._database_url = database_url
        self._owner: Any | None = None
        self._pool_connection: Any | None = None
        self._row_factory: Any = None
        self._acquire_lock = asyncio.Lock()
        # asyncpg permits only one in-flight operation per physical connection.
        # A request can legitimately start independent read helpers with
        # asyncio.gather(), so serialize the actual driver calls here instead
        # of exposing an intermittent InterfaceError to the whole endpoint.
        self._operation_lock = asyncio.Lock()
        self._closed = False

    @property
    def row_factory(self) -> Any:
        if self._owner is not None:
            return self._owner.row_factory
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._row_factory = value
        if self._owner is not None:
            self._owner.row_factory = value

    @property
    def acquired(self) -> bool:
        return self._owner is not None

    async def _ensure(self) -> Any:
        if self._closed:
            raise RuntimeError("request database connection is already closed")
        if self._owner is not None:
            return self._owner
        async with self._acquire_lock:
            if self._closed:
                raise RuntimeError("request database connection is already closed")
            if self._owner is not None:
                return self._owner
            if uses_postgres(self._database_url):
                if _pool is None:
                    await start(self._database_url)
                connection = await _pool.acquire()
                try:
                    owner = PostgresConnection(connection)
                    owner.row_factory = self._row_factory
                except BaseException:
                    await _pool.release(connection)
                    raise
                self._pool_connection = connection
                self._owner = owner
                return owner

            connection = await aiosqlite.connect(self._sqlite_path, timeout=5)
            try:
                await connection.execute("PRAGMA busy_timeout=5000")
                await connection.execute("PRAGMA foreign_keys=ON")
                if self._row_factory is not None:
                    connection.row_factory = self._row_factory
            except BaseException:
                await connection.close()
                raise
            self._owner = connection
            return connection

    async def commit(self) -> None:
        async with self._operation_lock:
            if self._owner is not None:
                await self._owner.commit()

    async def rollback(self) -> None:
        async with self._operation_lock:
            if self._owner is not None:
                await self._owner.rollback()

    async def begin_nested_write(self) -> Any | None:
        async with self._operation_lock:
            owner = await self._ensure()
            if isinstance(owner, PostgresConnection):
                return await owner.begin_nested()
            return None

    async def finish_nested(self, savepoint: Any, failed: bool) -> None:
        async with self._operation_lock:
            if failed:
                await savepoint.rollback()
            else:
                try:
                    await savepoint.commit()
                except BaseException:
                    # Do not leave the outer transaction in an aborted state if
                    # a savepoint commit fails and the caller handles it.
                    try:
                        await savepoint.rollback()
                    finally:
                        raise

    async def release(self) -> None:
        async with self._operation_lock:
            self._closed = True
            await self._release_owner()

    async def _release_owner(self) -> None:
        owner = self._owner
        if owner is None:
            return
        self._owner = None
        if uses_postgres(self._database_url):
            await _pool.release(self._pool_connection)
            self._pool_connection = None
        else:
            await owner.close()

    async def release_if_idle(self) -> bool:
        """Return a connection to the pool when no transaction is active.

        This is used immediately before slow provider I/O. A pending write is
        deliberately left attached to the request so its transaction boundary
        remains unchanged.
        """
        async with self._operation_lock:
            owner = self._owner
            if owner is None:
                return False
            if isinstance(owner, PostgresConnection):
                if owner.has_transaction:
                    return False
            elif getattr(owner, "in_transaction", False):
                return False
            await self._release_owner()
            return True

    def __getattr__(self, name: str) -> Any:
        async def invoke(*args: Any, **kwargs: Any) -> Any:
            async with self._operation_lock:
                owner = await self._ensure()
                return await getattr(owner, name)(*args, **kwargs)

        return invoke


def create_background_task(coro: Any, *, name: str | None = None) -> asyncio.Task:
    """Create a task without inheriting a request-owned DB connection."""
    context = contextvars.copy_context()
    context.run(_CURRENT_CONNECTION.set, None)
    return context.run(asyncio.create_task, coro, name=name)


async def release_request_connection_if_idle() -> bool:
    """Release the request DB lease before external I/O when it is safe."""
    connection = current_connection()
    if isinstance(connection, _BorrowedConnection):
        return await connection.release_if_idle()
    return False


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


# `?` is ambiguous: it is both the SQLite placeholder and a PostgreSQL jsonb
# operator. The project only uses it as a placeholder, so an operator in the
# SQL is a bug that must surface loudly rather than silently shift parameter
# numbering.
_JSONB_QUESTION_OPS = ("?|", "?&", "??")


def _postgres_sql(sql: str) -> str:
    """Rewrite SQLite `?` placeholders as PostgreSQL `$n`, respecting SQL lexis.

    A naive ``sql.split("?")`` renumbers every placeholder that follows a
    question mark inside a string literal (``LIKE '%?%'``), a quoted
    identifier, or a comment.  Nothing raises: asyncpg simply binds the wrong
    value to the wrong slot.  This scanner skips those regions instead.
    """
    out: list[str] = []
    index = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]

        if ch == "'":  # string literal, '' escapes a quote
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue

        if ch == '"':  # quoted identifier, "" escapes a quote
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue

        if ch == "$":  # PostgreSQL dollar-quoted body: $$...$$ or $tag$...$tag$
            end_tag = sql.find("$", i + 1)
            if end_tag != -1:
                tag = sql[i:end_tag + 1]
                body = tag[1:-1]
                if body == "" or body.replace("_", "a").isalnum():
                    close = sql.find(tag, end_tag + 1)
                    if close != -1:
                        out.append(sql[i:close + len(tag)])
                        i = close + len(tag)
                        continue

        if sql.startswith("--", i):  # line comment
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(sql[i:j])
            i = j
            continue

        if sql.startswith("/*", i):  # block comment; PostgreSQL allows nesting
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append(sql[i:j])
            i = j
            continue

        if ch == "?":
            for op in _JSONB_QUESTION_OPS:
                if sql.startswith(op, i):
                    raise ValueError(
                        f"jsonb operator {op!r} collides with `?` placeholders; "
                        "use jsonb_exists / jsonb_exists_any / jsonb_exists_all"
                    )
            index += 1
            out.append(f"${index}")
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


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

    Транзакция стартует лениво — только с первой записью. Обычные SELECT больше не
    платят за BEGIN/ROLLBACK, а операции с изменениями сохраняют прежний контракт:
    несколько execute() внутри одного `async with connect()` атомарны и требуют
    явный commit(), как в SQLite.
    """

    def __init__(self, connection: Any):
        self._connection = connection
        self._transaction: Any | None = None
        self._done = False  # rollback() after an error; normal commits may be repeated.
        self.row_factory: Any = None

    async def _ensure_transaction(self) -> None:
        if self._transaction is None:
            self._transaction = self._connection.transaction()
            await self._transaction.start()

    async def begin_nested(self) -> Any:
        """Start one SAVEPOINT inside the request transaction."""
        await self._ensure_transaction()
        nested = self._connection.transaction()
        await nested.start()
        return nested

    @property
    def has_transaction(self) -> bool:
        return self._transaction is not None

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> PostgresCursor:
        statement = sql.strip()
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("PRAGMA "):
            return PostgresCursor()

        statement = _postgres_sql(sql)
        query_type = statement.lstrip().split(None, 1)[0].upper()
        # SELECT — read-only fast path: asyncpg safely executes it without an
        # explicit transaction. WITH can contain mutations, therefore remains
        # transactional; all project writes with RETURNING also must be atomic.
        if query_type == "SELECT":
            rows = await self._connection.fetch(statement, *parameters)
            return PostgresCursor(rows, len(rows))
        await self._ensure_transaction()
        if query_type == "WITH" or " RETURNING " in statement.upper():
            rows = await self._connection.fetch(statement, *parameters)
            return PostgresCursor(rows, len(rows))
        status = await self._connection.execute(statement, *parameters)
        return PostgresCursor(rowcount=_rowcount(status))

    async def commit(self) -> None:
        """Commit the current unit of work and allow another one in this scope.

        SQLite permits multiple commits on one connection.  Keeping the same
        behaviour here matters for schema bootstrapping, where a base table is
        committed before an isolated compatibility migration opens another
        connection.  A later write must start and commit a fresh transaction,
        not be silently left open because an earlier commit marked the adapter
        as permanently done.
        """
        if self._done:
            return
        if self._transaction is not None:
            await self._transaction.commit()
            self._transaction = None

    async def rollback(self) -> None:
        if not self._done:
            if self._transaction is not None:
                await self._transaction.rollback()
                self._transaction = None
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
    borrowed = current_connection()
    if borrowed is not None:
        # SQLite keeps its historical borrowed-connection behaviour.  On
        # PostgreSQL each nested helper gets a lazy SAVEPOINT boundary.
        if not uses_postgres(database_url):
            yield borrowed
            return
        nested = _NestedBorrowedConnection(borrowed)
        try:
            yield nested
        except BaseException:
            await nested.finish(failed=True)
            raise
        else:
            await nested.finish(failed=False)
        return

    if uses_postgres(database_url):
        if _pool is None:
            await start(database_url)
        async with _pool.acquire() as connection:
            conn = PostgresConnection(connection)
            try:
                yield conn
            except BaseException:
                if not conn._done:
                    await conn.rollback()
                raise
            else:
                if not conn._done:  # забыли commit() — откатываем, а не тихо теряем контракт
                    await conn.rollback()
        return

    # A short busy timeout prevents transient "database is locked" failures when
    # several API requests contend for the single SQLite writer. Foreign keys are
    # connection-scoped in SQLite, so they must be enabled for every connection.
    async with aiosqlite.connect(sqlite_path, timeout=5) as connection:
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("PRAGMA foreign_keys=ON")
        yield connection


@asynccontextmanager
async def request_scope(sqlite_path: str, database_url: str) -> AsyncIterator[Any]:
    """Own one lazily acquired connection and transaction for an HTTP request."""
    existing = current_connection()
    if existing is not None:
        yield existing
        return

    lazy_owner = _LazyRequestConnection(sqlite_path, database_url)
    borrowed = _BorrowedConnection(lazy_owner)
    token = _CURRENT_CONNECTION.set(borrowed)
    try:
        yield borrowed
    except BaseException:
        await lazy_owner.rollback()
        raise
    else:
        try:
            await lazy_owner.commit()
        except BaseException:
            await lazy_owner.rollback()
            raise
    finally:
        _CURRENT_CONNECTION.reset(token)
        await lazy_owner.release()
