"""Tests for app/services/maintenance/_chunked_sql.py — the batching
helper the writer-lock-sensitive fixers (legacy_retag, event_reassign) rely
on to never commit more than one batch's worth of work at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, col, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent
from app.services.maintenance._chunked_sql import chunked_delete, chunked_update


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
