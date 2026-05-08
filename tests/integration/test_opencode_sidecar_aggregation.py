"""Integration tests for OpenCode._strategy_sidecar_aggregation DB-backed aggregation.

These tests verify that the sidecar fallback strategy correctly queries
UsageEvent rows and produces Combined cards grouped by account_id.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import sqlmodel as _sqlmodel_mod
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent
from app.services.collectors.opencode import OpenCodeCollector

# Capture the real Session class at import time, before any mocks are applied.
# The conftest.py autouse fixture patches sqlmodel.Session globally; we bypass
# that by holding a direct reference to the real class here.
_RealSession = _sqlmodel_mod.Session


@contextmanager
def _real_db(engine):
    """Context manager: patch app.core.db.engine AND restore the real sqlmodel.Session.

    The root conftest mocks sqlmodel.Session globally (autouse fixture).
    OpenCode._strategy_sidecar_aggregation uses `from sqlmodel import Session`
    at call time, which resolves to the mock unless we un-patch it here.
    """
    with patch("app.core.db.engine", engine), patch.object(_sqlmodel_mod, "Session", _RealSession):
        yield


@pytest.fixture
def in_memory_engine():
    """Provide an in-memory SQLite engine with all tables created."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _event(
    account_id: str,
    cost_usd: float,
    ts: datetime,
    sidecar_id: str = "host-a",
    event_id: str | None = None,
) -> UsageEvent:
    return UsageEvent(
        provider_id="opencode",
        account_id=account_id,
        sidecar_id=sidecar_id,
        event_id=event_id or f"evt_{account_id}_{ts.timestamp()}_{sidecar_id}",
        ts=ts,
        model_id="claude-sonnet-4-5",
        cost_usd=cost_usd,
    )


@pytest.mark.asyncio
async def test_opencode_combines_costs_across_sidecars(in_memory_engine):
    """Events from two sidecars for the same account are summed into one Combined card."""
    now = datetime.now(UTC)

    with _RealSession(in_memory_engine) as session:
        # Two events for the same account from different hosts, both within 5h window
        session.add(_event("user@example.com", 2.50, now - timedelta(hours=1), sidecar_id="host-a"))
        session.add(_event("user@example.com", 1.00, now - timedelta(hours=2), sidecar_id="host-b"))
        session.commit()

    collector = OpenCodeCollector()

    with _real_db(in_memory_engine):
        cards = await collector._strategy_sidecar_aggregation(client=None)

    session_cards = [c for c in cards if c["window_type"] == "session"]
    assert len(session_cards) == 1, f"Expected 1 session card, got {len(session_cards)}"
    card = session_cards[0]
    assert card["provider_id"] == "opencode"
    assert card["variant"] == "Combined"
    assert card["account_id"] == "user@example.com"
    # Cost should be the sum from both sidecars
    assert abs(card["used_value"] - 3.50) < 0.001, f"Expected 3.50, got {card['used_value']}"
    assert card["msgs"] == 2


@pytest.mark.asyncio
async def test_opencode_skips_window_with_no_events(in_memory_engine):
    """An account with events only in the weekly window produces no session card."""
    now = datetime.now(UTC)

    with _RealSession(in_memory_engine) as session:
        # Event outside the 5h session window but within 7d weekly window
        session.add(_event("user@example.com", 5.00, now - timedelta(hours=10)))
        session.commit()

    collector = OpenCodeCollector()

    with _real_db(in_memory_engine):
        cards = await collector._strategy_sidecar_aggregation(client=None)

    session_cards = [c for c in cards if c["window_type"] == "session"]
    weekly_cards = [c for c in cards if c["window_type"] == "weekly"]

    assert len(session_cards) == 0, "No session card expected — event is older than 5h"
    assert len(weekly_cards) == 1, "Weekly card expected for event within 7d"
    assert abs(weekly_cards[0]["used_value"] - 5.00) < 0.001


@pytest.mark.asyncio
async def test_opencode_two_accounts_produce_separate_cards(in_memory_engine):
    """Events for two different accounts produce independent Combined cards."""
    now = datetime.now(UTC)

    with _RealSession(in_memory_engine) as session:
        session.add(
            _event("alice@example.com", 1.00, now - timedelta(hours=1), sidecar_id="host-a")
        )
        session.add(_event("bob@example.com", 2.00, now - timedelta(hours=1), sidecar_id="host-b"))
        session.commit()

    collector = OpenCodeCollector()

    with _real_db(in_memory_engine):
        cards = await collector._strategy_sidecar_aggregation(client=None)

    session_cards = [c for c in cards if c["window_type"] == "session"]
    account_ids = {c["account_id"] for c in session_cards}
    assert "alice@example.com" in account_ids
    assert "bob@example.com" in account_ids
    assert len(session_cards) == 2


@pytest.mark.asyncio
async def test_opencode_returns_empty_when_no_events(in_memory_engine):
    """No UsageEvent rows → empty list (no crash)."""
    collector = OpenCodeCollector()

    with _real_db(in_memory_engine):
        cards = await collector._strategy_sidecar_aggregation(client=None)

    assert cards == []


@pytest.mark.asyncio
async def test_opencode_returns_empty_on_db_error():
    """A DB exception is caught and returns an empty list without propagating."""
    collector = OpenCodeCollector()

    # Patch engine to a MagicMock and Session to raise; the method should catch and return [].
    with patch("app.core.db.engine", MagicMock()):
        with patch.object(_sqlmodel_mod, "Session", side_effect=Exception("DB error")):
            cards = await collector._strategy_sidecar_aggregation(client=None)

    assert cards == []
