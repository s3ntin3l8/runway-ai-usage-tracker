"""Account-independent event identity: ``(provider_id, event_id)``.

The same message pushed under a new account (operator retag, hint arrived or
was withdrawn) must move, not double count. Covers the ingest-time
re-attribution rule and the one-shot migration of existing databases.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup
from app.models.schemas import UsageEventPush
from app.services.event_identity_migration import (
    INDEX_NAME,
    migrate_to_provider_event_identity,
)
from app.services.event_ingestor import EventIngestor
from app.services.period_rollups import update_rollups_for_event


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _push(account_id: str, event_id: str = "msg_1", kind: str = "message") -> UsageEventPush:
    return UsageEventPush(
        provider_id="anthropic",
        account_id=account_id,
        event_id=event_id,
        ts="2026-09-01T10:00:00Z",
        kind=kind,
        model_id="sonnet-4.5",
        tokens_input=100,
        tokens_output=10,
        error_reason="auth" if kind == "error" else None,
    )


def _lifetime(session: Session, account_id: str) -> tuple[int, int]:
    """(msgs, tokens_input) on the account's lifetime/all-models/all-sidecars grain."""
    row = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.account_id == account_id,
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    return (row.msgs, row.tokens_input) if row else (0, 0)


# ---------------------------------------------------------------------------
# Ingest-time re-attribution
# ---------------------------------------------------------------------------


def test_same_sidecar_retag_moves_event_and_rollups(session: Session):
    ing = EventIngestor(session)
    ing.ingest([_push("default")], sidecar_id="laptop")
    assert _lifetime(session, "default") == (1, 100)

    # The operator tags the credential; the sidecar re-pushes under the tag.
    res = EventIngestor(session).ingest([_push("alice@example.com")], sidecar_id="laptop")

    assert (res.events_inserted, res.events_reattributed, res.events_duplicate) == (0, 1, 0)
    rows = session.exec(select(UsageEvent)).all()
    assert [(r.account_id, r.event_id) for r in rows] == [("alice@example.com", "msg_1")]
    # Counted exactly once, on the new account.
    assert _lifetime(session, "alice@example.com") == (1, 100)
    assert _lifetime(session, "default") == (0, 0)


def test_other_sidecar_cannot_steal_an_event(session: Session):
    """A second host seeing the same log (shared home dir) keeps the first
    attribution — two hosts must not flip an event back and forth."""
    EventIngestor(session).ingest([_push("alice@example.com")], sidecar_id="laptop")
    res = EventIngestor(session).ingest([_push("bob@example.com")], sidecar_id="desktop")

    assert (res.events_inserted, res.events_reattributed, res.events_duplicate) == (0, 0, 1)
    row = session.exec(select(UsageEvent)).one()
    assert row.account_id == "alice@example.com"
    assert _lifetime(session, "bob@example.com") == (0, 0)


def test_identical_repush_is_a_plain_duplicate(session: Session):
    EventIngestor(session).ingest([_push("alice@example.com")], sidecar_id="laptop")
    res = EventIngestor(session).ingest([_push("alice@example.com")], sidecar_id="laptop")
    assert (res.events_inserted, res.events_reattributed, res.events_duplicate) == (0, 0, 1)
    assert _lifetime(session, "alice@example.com") == (1, 100)


def test_error_events_reattribute_without_rollups(session: Session):
    EventIngestor(session).ingest([_push("default", kind="error")], sidecar_id="laptop")
    res = EventIngestor(session).ingest(
        [_push("alice@example.com", kind="error")], sidecar_id="laptop"
    )
    assert res.events_reattributed == 1
    assert session.exec(select(UsageEvent)).one().account_id == "alice@example.com"
    assert session.exec(select(UsagePeriodRollup)).all() == []


# ---------------------------------------------------------------------------
# One-shot migration of existing databases
# ---------------------------------------------------------------------------


def _legacy_event(account_id: str, event_id: str, tokens: int = 100) -> UsageEvent:
    return UsageEvent(
        provider_id="anthropic",
        account_id=account_id,
        sidecar_id="laptop",
        event_id=event_id,
        ts=datetime(2026, 9, 1, 10, tzinfo=UTC),
        kind="message",
        model_id="sonnet-4.5",
        tokens_input=tokens,
    )


def _index_exists(session: Session) -> bool:
    return (
        session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"), {"n": INDEX_NAME}
        ).first()
        is not None
    )


def test_migration_collapses_cross_account_duplicates(session: Session):
    session.execute(text(f"DROP INDEX {INDEX_NAME}"))  # pre-migration DB
    # msg_1 double-counted under default + alice (a retag); msg_2 only default.
    for ev in (
        _legacy_event("default", "msg_1"),
        _legacy_event("alice@example.com", "msg_1"),
        _legacy_event("default", "msg_2", tokens=5),
    ):
        session.add(ev)
        session.flush()
        update_rollups_for_event(session, ev)
    session.commit()
    assert _lifetime(session, "default") == (2, 105)

    removed = migrate_to_provider_event_identity(session)

    assert removed == 1
    rows = sorted((r.event_id, r.account_id) for r in session.exec(select(UsageEvent)))
    # The real account wins over "default"; the lone row is untouched.
    assert rows == [("msg_1", "alice@example.com"), ("msg_2", "default")]
    assert _lifetime(session, "alice@example.com") == (1, 100)
    assert _lifetime(session, "default") == (1, 5)
    assert _index_exists(session)

    # Idempotent: index present → no-op.
    assert migrate_to_provider_event_identity(session) == 0


def test_migration_is_noop_on_fresh_db(session: Session):
    assert _index_exists(session)  # create_all builds it from the model
    assert migrate_to_provider_event_identity(session) == 0


# ---------------------------------------------------------------------------
# Sidecar: retagged events also advance the iterating provider's watermark
# ---------------------------------------------------------------------------


def test_retagged_events_register_watermark_alias(monkeypatch):
    import scripts.sidecar as sc

    evt = UsageEventPush(
        provider_id="minimax",  # retagged from opencode
        account_id="me@example.com",
        event_id="msg_abc",
        ts="2026-09-01T10:00:00Z",
        kind="message",
        model_id="minimax-m2",
    )
    monkeypatch.setattr(
        sc, "_make_account_extractor_opencode", lambda *_a, **_k: lambda *a, **k: [evt]
    )
    sc._EVENT_WATERMARK_ALIASES.clear()
    out: list[dict] = []
    sc._extract_events_for_provider(
        "opencode",
        ["default"],
        watermark=MagicMock(last_pushed=MagicMock(return_value=None)),
        bootstrap_days=90,
        out_events=out,
    )
    assert sc._EVENT_WATERMARK_ALIASES == {("minimax", "msg_abc"): ("opencode", "default")}
