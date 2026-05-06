"""Integration test: GET /api/v1/usage/fleet — Fleet Commander aggregation."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.main import app
from app.models.db import CumulativeUsage, LatestUsage


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


def _seed_card(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    sidecar_id: str = "local",
    window_type: str = "monthly",
    variant: str = "default",
    pct_used: float | None = None,
    service_name: str | None = None,
):
    card = {
        "service_name": service_name or f"{provider_id}-{window_type}",
        "provider_id": provider_id,
        "account_id": account_id,
        "window_type": window_type,
        "variant": variant,
        "pct_used": pct_used,
    }
    session.add(
        LatestUsage(
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
            window_type=window_type,
            variant=variant,
            card_json=json.dumps(card),
        )
    )


def test_fleet_picks_most_restrictive_card(session: Session):
    """When an account has multiple cards, the highest pct_used becomes the gauge."""
    _seed_card(
        session, provider_id="anthropic", account_id="acc1", window_type="weekly", pct_used=30.0
    )
    _seed_card(
        session, provider_id="anthropic", account_id="acc1", window_type="monthly", pct_used=85.0
    )
    _seed_card(
        session, provider_id="anthropic", account_id="acc1", window_type="session", pct_used=10.0
    )
    session.commit()

    client = TestClient(app)
    resp = client.get("/api/v1/usage/fleet")
    assert resp.status_code == 200, resp.text

    fleet = resp.json()["fleet"]
    assert len(fleet) == 1
    entry = fleet[0]
    assert entry["provider_id"] == "anthropic"
    assert entry["account_id"] == "acc1"
    assert entry["critical_gauge"]["pct_used"] == 85.0
    assert len(entry["secondary_limits"]) == 2


def test_fleet_groups_by_provider_account(session: Session):
    """Each (provider_id, account_id) gets its own Fleet Commander entry."""
    _seed_card(session, provider_id="anthropic", account_id="acc1", pct_used=50.0)
    _seed_card(session, provider_id="chatgpt", account_id="acc1", pct_used=20.0)
    session.commit()

    client = TestClient(app)
    resp = client.get("/api/v1/usage/fleet")

    fleet = resp.json()["fleet"]
    assert len(fleet) == 2
    pids = {e["provider_id"] for e in fleet}
    assert pids == {"anthropic", "chatgpt"}


def test_fleet_includes_sidecar_contributions(session: Session):
    """CumulativeUsage (current month) populates sidecar_contributions in the fleet view.

    Under the new constraint, the DB stores one merged row per logical identity; the
    row's sidecar_id reflects whichever sidecar was last merged into it. Per-sidecar
    breakdown in contributions is a Phase 3/4 write-path concern.
    """
    _seed_card(session, provider_id="anthropic", account_id="acc1", pct_used=10.0)

    month_key = datetime.now(UTC).strftime("%Y-%m")
    # One merged row — the write path (Phase 3) will have already summed contributions.
    session.add(
        CumulativeUsage(
            provider_id="anthropic",
            account_id="acc1",
            sidecar_id="laptop-1",
            period_type="month",
            period_key=month_key,
            unit_type="tokens_input",
            total_value=9234.0,
        )
    )
    session.commit()

    client = TestClient(app)
    resp = client.get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    contrib = resp.json()["fleet"][0]["sidecar_contributions"]
    # The merged row is exposed under its stored sidecar_id
    assert contrib["laptop-1"]["tokens_input"] == 9234.0
