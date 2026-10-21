"""Let the test suite write to its throwaway SQLite databases without fsyncing.

The suite builds its own throwaway SQLite engines instead of reusing the app
engine: a fixture creates a fresh file, runs ``SQLModel.metadata.create_all()``
and commits a handful of rows, then throws the file away. Those engines never
go through ``app.core.db.configure_sqlite_engine``, so they keep SQLite's
default ``synchronous=FULL`` and fsync on every commit.

On an ordinary disk that made the suite I/O-bound rather than CPU-bound: with
``synchronous=OFF`` the same three modules run 4x faster (80.6s -> 19.0s) and
the local run goes from 19% CPU to 46%, because the wall time was spent
waiting on the filesystem instead of running code.

The app engine is deliberately exempt. It sets ``synchronous=NORMAL`` and WAL
on purpose (``app/core/db.py``) because those are production decisions about
durability and concurrency, and a test suite that quietly changed them would
stop exercising the configuration that ships.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

# Set to "1" to restore SQLite's default (durable) behaviour for a run.
SKIP_FSYNC_ENV_VAR = "RUNWAY_TEST_SQLITE_SYNC"

#: ``PRAGMA synchronous`` levels, as SQLite reports them.
SYNCHRONOUS_OFF = 0
SYNCHRONOUS_NORMAL = 1
SYNCHRONOUS_FULL = 2


def skip_fsync_requested() -> bool:
    """Whether this run asked for durable writes back."""
    return os.environ.get(SKIP_FSYNC_ENV_VAR) == "1"


def app_database_path() -> str:
    """The file the app's own engine writes to ("" when it has none)."""
    from app.core.db import engine as app_engine

    database = app_engine.url.database or ""
    if database in ("", ":memory:"):
        return ""
    return os.path.abspath(database)


def fast_write_path_applies(dialect_name: str, database_file: str) -> bool:
    """Whether the fast (non-fsyncing) write path applies to a connection.

    Two exclusions, both deliberate:

    * the app's own database, which configures its own durability settings;
    * any non-SQLite dialect, where ``PRAGMA synchronous`` means nothing.
    """
    if dialect_name != "sqlite":
        return False
    if not database_file:
        # An in-memory database has nothing to fsync in the first place.
        return True
    return os.path.abspath(database_file) != app_database_path()


def _database_file(dbapi_connection: sqlite3.Connection) -> str:
    """The file the connection is attached to ("" for an in-memory database)."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA database_list")
        rows = cursor.fetchall()
    finally:
        cursor.close()
    for row in rows:
        if row[1] == "main":
            return row[2] or ""
    return ""


def disable_fsync(dbapi_connection: Any, _connection_record: Any) -> None:
    """``connect`` listener that turns fsyncing off for the suite's engines."""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    if not fast_write_path_applies("sqlite", _database_file(dbapi_connection)):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA synchronous=OFF")
    finally:
        cursor.close()
