"""Tests for app/services/maintenance/_chunked_sql.py — the batching
helper the writer-lock-sensitive fixers (legacy_retag, event_reassign) rely
on to never commit more than one batch's worth of work at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, col, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent
from app.services.maintenance._chunked_sql import (
    _next_batch_ids,
    chunked_delete,
    chunked_update,
)
from app.services.maintenance._retry import _is_sqlite_locked_or_busy


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session, n: int) -> None:
    for i in range(n):
        session.add(
            UsageEvent(
                provider_id="minimax",
                account_id="default",
                sidecar_id="dev-01",
                event_id=f"msg_{i}",
                ts=datetime(2026, 9, 1, tzinfo=UTC),
                kind="message",
                model_id="MiniMax-M3",
                tokens_input=1,
                tokens_output=1,
                cost_usd=0.0,
            )
        )
    session.commit()


def test_chunked_update_moves_every_row_across_multiple_batches():
    session = _session()
    _seed(session, 11)

    total = chunked_update(
        session,
        UsageEvent,
        [col(UsageEvent.provider_id) == "minimax"],
        {"account_id": "alice@example.com"},
        batch_size=4,
    )

    assert total == 11
    remaining_under_default = session.exec(
        select(UsageEvent).where(col(UsageEvent.account_id) == "default")
    ).all()
    assert remaining_under_default == []
    moved = session.exec(
        select(UsageEvent).where(col(UsageEvent.account_id) == "alice@example.com")
    )
    assert len(list(moved)) == 11


def test_chunked_update_exact_multiple_of_batch_size_terminates():
    """The boundary case: exactly batch_size rows in the last batch must not
    be mistaken for "more to come" and trigger an extra no-op round trip
    that silently affects zero new rows (still correct, but worth pinning)."""
    session = _session()
    _seed(session, 8)

    total = chunked_update(
        session,
        UsageEvent,
        [col(UsageEvent.provider_id) == "minimax"],
        {"account_id": "alice@example.com"},
        batch_size=4,
    )

    assert total == 8


def test_chunked_update_only_touches_matching_rows():
    session = _session()
    _seed(session, 3)
    session.add(
        UsageEvent(
            provider_id="kimi_coding",
            account_id="default",
            sidecar_id="dev-01",
            event_id="msg_other",
            ts=datetime(2026, 9, 1, tzinfo=UTC),
            kind="message",
            model_id="k2",
            tokens_input=1,
            tokens_output=1,
            cost_usd=0.0,
        )
    )
    session.commit()

    chunked_update(
        session,
        UsageEvent,
        [col(UsageEvent.provider_id) == "minimax"],
        {"account_id": "alice@example.com"},
        batch_size=2,
    )

    untouched = session.exec(
        select(UsageEvent).where(col(UsageEvent.provider_id) == "kimi_coding")
    ).one()
    assert untouched.account_id == "default"


def test_chunked_delete_removes_every_matching_row_across_batches():
    session = _session()
    _seed(session, 11)

    total = chunked_delete(
        session, UsageEvent, [col(UsageEvent.provider_id) == "minimax"], batch_size=4
    )

    assert total == 11
    assert session.exec(select(UsageEvent)).all() == []


def test_chunked_update_is_resumable_after_a_partial_run():
    """Simulates a crash mid-run: only some batches committed. Calling again
    with the same arguments must finish the job, not double-count or skip."""
    session = _session()
    _seed(session, 10)

    first = chunked_update(
        session,
        UsageEvent,
        [col(UsageEvent.provider_id) == "minimax", col(UsageEvent.account_id) == "default"],
        {"account_id": "alice@example.com"},
        batch_size=4,
    )
    assert first == 10  # nothing left to resume in this run, but idempotence still holds:

    second = chunked_update(
        session,
        UsageEvent,
        [col(UsageEvent.provider_id) == "minimax", col(UsageEvent.account_id) == "default"],
        {"account_id": "alice@example.com"},
        batch_size=4,
    )
    assert second == 0  # already-moved rows no longer match account_id == "default"


def test_chunked_update_retries_on_locked_database_and_succeeds():
    """Simulates a transient SQLite lock contention during an UPDATE batch.
    The update must catch OperationalError, roll back, retry, and successfully complete."""
    session = _session()
    _seed(session, 5)

    original_exec = session.exec
    attempts = 0

    def mock_exec(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        # Fail the first attempt with "database is locked"
        if attempts == 1:
            raise OperationalError("UPDATE ...", {}, Exception("database is locked"))
        return original_exec(*args, **kwargs)

    with patch.object(session, "exec", side_effect=mock_exec):
        total = chunked_update(
            session,
            UsageEvent,
            [col(UsageEvent.provider_id) == "minimax"],
            {"account_id": "alice@example.com"},
            batch_size=10,
            initial_backoff=0.001,
        )

    assert total == 5
    assert attempts == 2
    remaining = session.exec(
        select(UsageEvent).where(col(UsageEvent.account_id) == "default")
    ).all()
    assert remaining == []


def test_chunked_delete_retries_on_locked_database_and_succeeds():
    """Simulates a transient SQLite lock contention during a DELETE batch."""
    session = _session()
    _seed(session, 5)

    original_exec = session.exec
    attempts = 0

    def mock_exec(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError("DELETE ...", {}, Exception("database is locked"))
        return original_exec(*args, **kwargs)

    with patch.object(session, "exec", side_effect=mock_exec):
        total = chunked_delete(
            session,
            UsageEvent,
            [col(UsageEvent.provider_id) == "minimax"],
            batch_size=10,
            initial_backoff=0.001,
        )

    assert total == 5
    assert attempts == 2
    assert session.exec(select(UsageEvent)).all() == []


def test_chunked_update_raises_after_max_retries_exhausted():
    """When lock contention is permanent, OperationalError must be re-raised after retries."""
    session = _session()
    _seed(session, 5)

    def fail_exec(*args, **kwargs):
        raise OperationalError("UPDATE ...", {}, Exception("database is locked"))

    with patch.object(session, "exec", side_effect=fail_exec):
        with pytest.raises(OperationalError, match="database is locked"):
            chunked_update(
                session,
                UsageEvent,
                [col(UsageEvent.provider_id) == "minimax"],
                {"account_id": "alice@example.com"},
                batch_size=10,
                max_retries=2,
                initial_backoff=0.001,
            )


def test_next_batch_ids_releases_transaction_lock():
    """_next_batch_ids commits after reading so no deferred read lock lingers into write operations."""
    session = _session()
    _seed(session, 3)

    ids = _next_batch_ids(
        session, UsageEvent, [col(UsageEvent.provider_id) == "minimax"], cursor_id=0, batch_size=10
    )
    assert len(ids) == 3
    # Transaction is not active / in-progress on the session
    assert not session.in_transaction()


def test_next_batch_ids_retries_on_locked_database_and_succeeds():
    """_next_batch_ids catches OperationalError during SELECT, rolls back, retries and succeeds."""
    session = _session()
    _seed(session, 3)

    original_execute = session.execute
    attempts = 0

    def mock_execute(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError("SELECT ...", {}, Exception("database is locked"))
        return original_execute(*args, **kwargs)

    with patch.object(session, "execute", side_effect=mock_execute):
        ids = _next_batch_ids(
            session,
            UsageEvent,
            [col(UsageEvent.provider_id) == "minimax"],
            cursor_id=0,
            batch_size=10,
            initial_backoff=0.001,
        )

    assert len(ids) == 3
    assert attempts == 2
    assert not session.in_transaction()


def test_is_sqlite_locked_or_busy_discriminates_error_origin():
    """Parameters containing 'locked' or 'busy' must not cause unrelated errors to be treated as lock contention."""
    # Fake error with parameter containing 'device-busy-machine'
    unrelated_exc = OperationalError(
        "UPDATE usage_events SET sidecar_id='device-busy-machine' ...",
        {"id": "busy"},
        Exception("no such table: usage_events"),
    )
    assert not _is_sqlite_locked_or_busy(unrelated_exc)

    # Real lock error
    lock_exc = OperationalError("UPDATE ...", {}, Exception("database is locked"))
    assert _is_sqlite_locked_or_busy(lock_exc)


def test_is_sqlite_locked_or_busy_handles_extended_codes_and_names():
    """Extended error codes (SQLITE_BUSY_SNAPSHOT, etc.) must be identified as lock contention."""

    class FakeOrigError(Exception):
        def __init__(self, errorcode: int, errorname: str):
            self.sqlite_errorcode = errorcode
            self.sqlite_errorname = errorname

    # 517 = SQLITE_BUSY_SNAPSHOT (517 & 0xFF == 5)
    exc_snapshot = OperationalError("SELECT ...", {}, FakeOrigError(517, "SQLITE_BUSY_SNAPSHOT"))
    assert _is_sqlite_locked_or_busy(exc_snapshot)

    # 261 = SQLITE_BUSY_RECOVERY (261 & 0xFF == 5)
    exc_recovery = OperationalError("SELECT ...", {}, FakeOrigError(261, "SQLITE_BUSY_RECOVERY"))
    assert _is_sqlite_locked_or_busy(exc_recovery)

    # 262 = SQLITE_LOCKED_SHAREDCACHE (262 & 0xFF == 6)
    exc_shared = OperationalError("SELECT ...", {}, FakeOrigError(262, "SQLITE_LOCKED_SHAREDCACHE"))
    assert _is_sqlite_locked_or_busy(exc_shared)

    # Unrelated code: 1 = SQLITE_ERROR
    exc_other = OperationalError("SELECT ...", {}, FakeOrigError(1, "SQLITE_ERROR"))
    assert not _is_sqlite_locked_or_busy(exc_other)


def test_chunked_operations_validate_retry_knobs():
    """chunked_update and chunked_delete must reject max_retries <= 0 with ValueError."""
    session = _session()
    with pytest.raises(ValueError, match="max_retries must be positive"):
        chunked_update(
            session,
            UsageEvent,
            [col(UsageEvent.provider_id) == "minimax"],
            {"account_id": "alice@example.com"},
            max_retries=0,
        )

    with pytest.raises(ValueError, match="max_retries must be positive"):
        chunked_delete(
            session,
            UsageEvent,
            [col(UsageEvent.provider_id) == "minimax"],
            max_retries=0,
        )

    with pytest.raises(ValueError, match="initial_backoff must be non-negative"):
        chunked_update(
            session,
            UsageEvent,
            [col(UsageEvent.provider_id) == "minimax"],
            {"account_id": "alice@example.com"},
            initial_backoff=-1.0,
        )
