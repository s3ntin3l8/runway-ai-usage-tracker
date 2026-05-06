"""Integration test: GET /api/v1/usage/cumulative rolls up CumulativeUsage rows."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.main import app
from app.models.db import CumulativeUsage


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as s:
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


def _seed(session: Session, **fields):
    """Insert a CumulativeUsage row, defaulting unspecified fields."""
    defaults = {
        "provider_id": "anthropic",
        "account_id": "acc1",
        "sidecar_id": "laptop-1",
        "period_type": "lifetime",
        "period_key": "all",
        "unit_type": "tokens_input",
        "total_value": 0.0,
        "last_updated": datetime.now(UTC),
    }
    defaults.update(fields)
    session.add(CumulativeUsage(**defaults))


def test_cumulative_exposes_unit_types_separately(session: Session):
    """Multiple unit_types for the same (provider, account) are exposed as separate keys.

    Cross-sidecar merge now happens at the write path (Phase 3) before rows reach
    the DB — the DB stores one already-merged row per logical identity.
    """
    # Single merged row (as the write path will produce after Phase 3)
    _seed(session, unit_type="tokens_input", total_value=500.0)
    # Different unit_type — should NOT merge with the above
    _seed(session, unit_type="cost_usd", total_value=2.5)
    session.commit()

    client = TestClient(app)
    resp = client.get("/api/v1/usage/cumulative")
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert "cumulative" in data
    assert len(data["cumulative"]) == 1

    entry = data["cumulative"][0]
    assert entry["provider_id"] == "anthropic"
    assert entry["account_id"] == "acc1"

    # Lifetime bucket exposes both unit_types
    lifetime = entry["lifetime"]
    assert lifetime["tokens_input"] == 500.0
    assert lifetime["cost_usd"] == 2.5


def test_cumulative_returns_lifetime_year_month_keys(session: Session):
    """Response always exposes lifetime, current-year, and current-month buckets."""
    now = datetime.now(UTC)
    year_key = now.strftime("%Y")
    month_key = now.strftime("%Y-%m")

    _seed(session, period_type="lifetime", period_key="all", total_value=1000.0)
    _seed(session, period_type="year", period_key=year_key, total_value=300.0)
    _seed(session, period_type="month", period_key=month_key, total_value=80.0)
    session.commit()

    client = TestClient(app)
    resp = client.get("/api/v1/usage/cumulative")
    assert resp.status_code == 200

    entry = resp.json()["cumulative"][0]
    assert entry["lifetime"]["tokens_input"] == 1000.0
    assert entry[f"year_{year_key}"]["tokens_input"] == 300.0
    assert entry[f"month_{month_key}"]["tokens_input"] == 80.0


def test_cumulative_filters_by_provider_and_period(session: Session):
    """Query params narrow the result set."""
    _seed(session, provider_id="anthropic", total_value=10.0)
    _seed(session, provider_id="chatgpt", total_value=20.0)
    session.commit()

    client = TestClient(app)
    resp = client.get("/api/v1/usage/cumulative?provider_id=chatgpt")
    assert resp.status_code == 200

    cumulative = resp.json()["cumulative"]
    assert len(cumulative) == 1
    assert cumulative[0]["provider_id"] == "chatgpt"
    assert cumulative[0]["lifetime"]["tokens_input"] == 20.0


def test_cumulative_empty_db_returns_empty_list(session: Session):
    """Endpoint shape stays sane on a fresh DB."""
    client = TestClient(app)
    resp = client.get("/api/v1/usage/cumulative")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cumulative"] == []
    assert "generated_at" in body
