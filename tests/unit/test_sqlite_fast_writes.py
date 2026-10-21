"""The suite's own SQLite engines must not fsync; the app's own database must.

Regression test for a suite-wide performance bug: the throwaway engines the
fixtures build never went through ``app.core.db.configure_sqlite_engine``, so
they kept SQLite's default ``synchronous=FULL`` and fsynced on every commit.
That made the suite I/O-bound -- profiling showed roughly 80% of the local
run's wall time spent waiting on the filesystem instead of running code.

The exemption for the app's own database matters just as much: it sets
``synchronous=NORMAL`` and WAL on purpose, and the suite must not quietly
change a production durability decision to buy speed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from tests.fixtures.sqlite import (
    SYNCHRONOUS_FULL,
    SYNCHRONOUS_NORMAL,
    SYNCHRONOUS_OFF,
    app_database_path,
    fast_write_path_applies,
    skip_fsync_requested,
)


def _synchronous_level(engine) -> int:
    """The ``PRAGMA synchronous`` level the engine's connections actually use."""
    with engine.connect() as connection:
        return connection.exec_driver_sql("PRAGMA synchronous").scalar()


def test_suite_owned_engine_does_not_fsync(tmp_path):
    """A fixture that builds its own engine must not pay an fsync per commit."""
    if skip_fsync_requested():
        pytest.skip("durable writes requested for this run")
    engine = create_engine(f"sqlite:///{tmp_path / 'probe.db'}")
    try:
        assert _synchronous_level(engine) == SYNCHRONOUS_OFF
    finally:
        engine.dispose()


def test_app_database_is_exempt_from_the_fast_path():
    """The app's own database keeps ``synchronous=NORMAL`` -- don't override it."""
    assert app_database_path() != ""
    assert fast_write_path_applies("sqlite", app_database_path()) is False


def test_suite_owned_database_gets_the_fast_path(tmp_path):
    """A throwaway file is not the app's database, so it gets the fast path."""
    assert fast_write_path_applies("sqlite", str(tmp_path / "probe.db")) is True


def test_non_sqlite_dialects_are_left_alone():
    """``PRAGMA synchronous`` means nothing outside SQLite."""
    assert fast_write_path_applies("postgresql", "/tmp/probe.db") is False


def test_in_memory_database_gets_the_fast_path():
    """An in-memory database has no file to fsync; the fast path is harmless."""
    assert fast_write_path_applies("sqlite", "") is True


def test_durable_writes_can_be_requested(monkeypatch):
    """``RUNWAY_TEST_SQLITE_SYNC=1`` opts a run back into the durable default."""
    monkeypatch.delenv("RUNWAY_TEST_SQLITE_SYNC", raising=False)
    assert skip_fsync_requested() is False
    monkeypatch.setenv("RUNWAY_TEST_SQLITE_SYNC", "1")
    assert skip_fsync_requested() is True


@pytest.mark.parametrize(
    ("level", "name"),
    [(SYNCHRONOUS_OFF, "OFF"), (SYNCHRONOUS_NORMAL, "NORMAL"), (SYNCHRONOUS_FULL, "FULL")],
)
def test_documented_synchronous_levels(level, name):
    """The level constants must match SQLite's own numbering."""
    assert level in {0, 1, 2, 3}, name
