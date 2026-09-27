"""Integration test for scripts/assign_default_events.py.

The script is a thin CLI wrapper over
app/services/maintenance/event_reassign.py — these tests exercise it through
its own `assign_events` entry point (patching the module-level `engine` and
`init_db`) rather than re-testing event_reassign.py's own logic, which has
its own dedicated unit tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import ProviderConfig, UsageEvent

NOW = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def mock_db_session():
    """Override the conftest autouse Session mock — this test needs a real DB."""
    yield


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _ev(session: Session, event_id: str, **overrides) -> UsageEvent:
    base = {
        "provider_id": "minimax",
        "account_id": "default",
        "sidecar_id": "dev-01",
        "event_id": event_id,
        "ts": NOW,
        "kind": "message",
        "model_id": "MiniMax-M3",
        "tokens_input": 100,
        "tokens_output": 20,
        "cost_usd": 0.05,
    }
    base.update(overrides)
    ev = UsageEvent(**base)
    session.add(ev)
    session.commit()
    return ev


def test_assign_events_moves_the_named_events(engine):
    with Session(engine) as s:
        s.add(ProviderConfig(provider_id="minimax", account_id="alice@example.com"))
        _ev(s, "e1")
        _ev(s, "e2")
        s.commit()

    with (
        patch("scripts.assign_default_events.engine", engine),
        patch("scripts.assign_default_events.init_db"),
    ):
        from scripts.assign_default_events import assign_events

        moved = assign_events("minimax", "alice@example.com", ["e1"], apply=True)

    assert moved == 1
    with Session(engine) as s:
        rows = {r.event_id: r.account_id for r in s.exec(select(UsageEvent)).all()}
        assert rows == {"e1": "alice@example.com", "e2": "default"}


def test_assign_events_dry_run_writes_nothing(engine):
    with Session(engine) as s:
        s.add(ProviderConfig(provider_id="minimax", account_id="alice@example.com"))
        _ev(s, "e1")
        s.commit()

    with (
        patch("scripts.assign_default_events.engine", engine),
        patch("scripts.assign_default_events.init_db"),
    ):
        from scripts.assign_default_events import assign_events

        count = assign_events("minimax", "alice@example.com", ["e1"], apply=False)

    assert count == 1
    with Session(engine) as s:
        assert s.exec(select(UsageEvent)).one().account_id == "default"


def test_assign_events_requires_a_configured_target_account(engine):
    with Session(engine) as s:
        _ev(s, "e1")
        s.commit()

    with (
        patch("scripts.assign_default_events.engine", engine),
        patch("scripts.assign_default_events.init_db"),
    ):
        from scripts.assign_default_events import assign_events

        with pytest.raises(ValueError, match="No configured account"):
            assign_events("minimax", "alice@example.com", ["e1"], apply=True)


def test_assign_events_rejects_a_missing_event_id(engine):
    with Session(engine) as s:
        s.add(ProviderConfig(provider_id="minimax", account_id="alice@example.com"))
        _ev(s, "e1")
        s.commit()

    with (
        patch("scripts.assign_default_events.engine", engine),
        patch("scripts.assign_default_events.init_db"),
    ):
        from scripts.assign_default_events import assign_events

        with pytest.raises(ValueError, match="nonexistent"):
            assign_events("minimax", "alice@example.com", ["e1", "nonexistent"], apply=True)


def test_assign_events_requires_at_least_one_event_id(engine):
    with (
        patch("scripts.assign_default_events.engine", engine),
        patch("scripts.assign_default_events.init_db"),
    ):
        from scripts.assign_default_events import assign_events

        with pytest.raises(ValueError, match="one or more"):
            assign_events("minimax", "alice@example.com", [], apply=True)
