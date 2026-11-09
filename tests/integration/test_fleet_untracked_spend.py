"""GET /api/v1/usage/fleet — Ollama allowance spend no tracked event explains."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import LatestUsage, UsageEvent
from tests.fixtures.test_client import TestClient

ACCOUNT = "dev@example.com"
NOW = datetime.now(UTC)
PERIOD_FROM = NOW - timedelta(days=10)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


def _seed_allowance(session: Session, *, spent: float, allowance: float = 10.0) -> None:
    card = {
        "service_name": "Ollama Cloud",
        "provider_id": "ollama",
        "account_id": ACCOUNT,
        "window_type": "monthly",
        "variant": "allowance",
        "unit_type": "currency",
        "currency": "USD",
        "used_value": spent,
        "limit_value": allowance,
        "pct_used": spent / allowance * 100,
        "detail": f"${spent:.2f} of ${allowance:.2f} used",
        "period_from": PERIOD_FROM.isoformat(),
        "reset_at": (NOW + timedelta(days=20)).isoformat(),
    }
    session.add(
        LatestUsage(
            provider_id="ollama",
            account_id=ACCOUNT,
            sidecar_id="local",
            window_type="monthly",
            variant="allowance",
            model_id="",
            card_json=json.dumps(card),
        )
    )
    session.commit()


def _seed_event(session: Session, n: int, cost: float, ts: datetime) -> None:
    session.add(
        UsageEvent(
            provider_id="ollama",
            account_id=ACCOUNT,
            event_id=f"evt-{n}",
            kind="message",
            ts=ts,
            cost_usd=cost,
        )
    )
    session.commit()


def _card(session: Session) -> dict:

    body = TestClient(app).get("/api/v1/usage/fleet").json()
    (entry,) = [e for e in body["fleet"] if e["provider_id"] == "ollama"]
    return entry["critical_gauge"]


@pytest.fixture(autouse=True)
def _no_fleet_cache(monkeypatch):
    monkeypatch.setattr("app.api.endpoints.usage_overview.cache_get", lambda key: None)


def test_gap_is_reported_with_detail_suffix(session):
    _seed_allowance(session, spent=10.0)
    _seed_event(session, 1, 9.20, NOW - timedelta(days=1))
    card = _card(session)
    assert card["untracked_spend_usd"] == 0.80
    assert card["detail"].endswith("· $0.80 not from tracked clients")


def test_small_gap_is_ignored(session):
    _seed_allowance(session, spent=10.0)
    _seed_event(session, 1, 9.98, NOW - timedelta(days=1))
    card = _card(session)
    assert card["untracked_spend_usd"] is None
    assert "not from tracked clients" not in card["detail"]


def test_no_events_is_not_measurable(session):
    _seed_allowance(session, spent=10.0)
    assert _card(session)["untracked_spend_usd"] is None


def test_only_unpriced_events_is_not_measurable(session):
    _seed_allowance(session, spent=10.0)
    _seed_event(session, 1, 0.0, NOW - timedelta(days=1))
    assert _card(session)["untracked_spend_usd"] is None


def test_events_outside_the_period_are_not_counted(session):
    _seed_allowance(session, spent=10.0)
    _seed_event(session, 1, 9.20, NOW - timedelta(days=1))
    _seed_event(session, 2, 5.00, PERIOD_FROM - timedelta(days=1))
    assert _card(session)["untracked_spend_usd"] == 0.80
