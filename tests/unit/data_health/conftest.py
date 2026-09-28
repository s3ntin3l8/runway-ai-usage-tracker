"""Shared fixture factories for app/services/data_health/ tests — an
in-memory SQLite session plus small builders for the rows each check
queries, mirroring the `_session()`/`_event()` convention used throughout
`tests/unit/test_maintenance_*.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import (
    CredentialTag,
    LatestUsage,
    ProviderConfig,
    ProviderPricing,
    QuotaSnapshot,
    UsageEvent,
)


@dataclass
class QueryCounter:
    """Counts SQL statements issued through a given engine, via
    `before_cursor_execute`. Reset per test by the `query_counter` fixture."""

    count: int = 0
    statements: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.count = 0
        self.statements.clear()


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


@pytest.fixture
def query_counter(session: Session) -> QueryCounter:
    """A SQL query counter hooked onto `session`'s own engine — use to
    assert a check's `detect()` issues a constant number of queries
    regardless of how many pairs/groups are in the DB."""
    counter = QueryCounter()
    engine = session.get_bind()

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        counter.count += 1
        counter.statements.append(statement)

    yield counter

    event.remove(engine, "before_cursor_execute", _count)


def make_event(session: Session, *, event_id: str = "x", **overrides) -> UsageEvent:
    base = {
        "provider_id": "minimax",
        "account_id": "default",
        "sidecar_id": "dev-01",
        "event_id": f"msg_{event_id}",
        "ts": datetime(2026, 9, 1, tzinfo=UTC),
        "kind": "message",
        "model_id": "MiniMax-M3",
        "tokens_input": 100,
        "tokens_output": 20,
        "cost_usd": 0.05,
        "attribution_source": "default",
    }
    base.update(overrides)
    ev = UsageEvent(**base)
    session.add(ev)
    session.commit()
    return ev


def make_config(
    session: Session, *, provider_id: str, account_id: str, **overrides
) -> ProviderConfig:
    base: dict = {"provider_id": provider_id, "account_id": account_id}
    base.update(overrides)
    row = ProviderConfig(**base)
    session.add(row)
    session.commit()
    return row


def make_tag(
    session: Session, *, provider_id: str, credential_origin: str, account_id: str, **overrides
) -> CredentialTag:
    base: dict = {
        "provider_id": provider_id,
        "credential_origin": credential_origin,
        "account_id": account_id,
    }
    base.update(overrides)
    row = CredentialTag(**base)
    session.add(row)
    session.commit()
    return row


def make_latest_usage(
    session: Session, *, provider_id: str, account_id: str, **overrides
) -> LatestUsage:
    base = {
        "provider_id": provider_id,
        "account_id": account_id,
        "window_type": "daily",
        "variant": "",
        "model_id": "",
        "card_json": "{}",
    }
    base.update(overrides)
    row = LatestUsage(**base)
    session.add(row)
    session.commit()
    return row


def make_snapshot(
    session: Session, *, provider_id: str, account_id: str, ts: datetime, **overrides
) -> QuotaSnapshot:
    base = {
        "provider_id": provider_id,
        "account_id": account_id,
        "window_type": "daily",
        "variant": "",
        "model_id": "",
        "ts": ts,
        "pct_used": 10.0,
    }
    base.update(overrides)
    row = QuotaSnapshot(**base)
    session.add(row)
    session.commit()
    return row


def make_price(
    session: Session, *, provider_id: str, model_id: str, rate: float = 1.0, **overrides
) -> ProviderPricing:
    from datetime import date

    base: dict = {
        "provider_id": provider_id,
        "model_id": model_id,
        "effective_from": date(2020, 1, 1),
        "input_per_mtok": rate,
        "output_per_mtok": rate,
        "cache_read_per_mtok": 0.0,
        "cache_create_per_mtok": 0.0,
    }
    base.update(overrides)
    row = ProviderPricing(**base)
    session.add(row)
    session.commit()
    return row
