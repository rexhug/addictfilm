"""Single entry point for opening a database connection.

Every repository module needs the connection settings, and those settings are
mutated at runtime: 35 test files rebind ``database.DB_PATH`` to a temporary
file before each case.  Binding a copy at import time would leave those tests
silently talking to the real database, so the attribute is read at call time
instead.

The ``import database`` is deliberately deferred into the function bodies:
``database`` imports this module, so a top-level import here would be circular.
Deferring it also guarantees the read happens after any test has rebound the
value.
"""
from __future__ import annotations

import db_runtime


def connect():
    """Open a connection using whatever configuration `database` holds now."""
    import database
    return db_runtime.connect(database.DB_PATH, database.DATABASE_URL)


def uses_postgres() -> bool:
    """True when the active backend is PostgreSQL rather than local SQLite."""
    import database
    return database._PG
