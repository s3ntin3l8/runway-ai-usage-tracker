"""API-level tests: usage_overview serves derived stale_reason on stale cards."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import CredentialSource, LatestUsage, ProviderConfig
from tests.fixtures.test_client import TestClient

PROVIDER = "anthropic_console"
ACCOUNT = "org-123"


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


@pytest.fixture(autouse=True)
def _no_fleet_cache(monkeypatch):
    monkeypatch.setattr("app.api.endpoints.usage_overview.cache_get", lambda key: None)


def _seed_card(
    session: Session,
    *,
    variant: str = "balance",
    stale: bool = True,
    pct_used: float = 50.0,
) -> None:
    card = {
        "service_name": "Claude Console",
        "provider_id": PROVIDER,
        "account_id": ACCOUNT,
        "window_type": "monthly",
        "variant": variant,
        "pct_used": pct_used,
        "stale": stale,
        "collection_failing": stale,
        "detail": "Collection failing: No credential",
    }
    session.add(
        LatestUsage(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            sidecar_id="local",
            window_type="monthly",
            variant=variant,
            model_id="",
            card_json=json.dumps(card),
        )
    )
    session.commit()


def test_fleet_and_limits_serve_no_credential_stale_reason(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    _seed_card(session, variant="balance", stale=True, pct_used=80.0)
    _seed_card(session, variant="secondary", stale=True, pct_used=20.0)

    client = TestClient(app)

    fleet_body = client.get("/api/v1/usage/fleet").json()
    (entry,) = [e for e in fleet_body["fleet"] if e["provider_id"] == PROVIDER]
    assert entry["critical_gauge"]["stale_reason"] == "no_credential"
    assert entry["secondary_limits"][0]["stale_reason"] == "no_credential"

    limits_body = client.get("/api/v1/usage/limits").json()
    assert all(c["stale_reason"] == "no_credential" for c in limits_body["limits"])

    # Never persisted on LatestUsage.card_json
    rows = session.exec(select(LatestUsage)).all()
    assert all("stale_reason" not in json.loads(r.card_json) for r in rows)


def test_fleet_and_limits_serve_credential_failing_stale_reason(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    session.add(
        CredentialSource(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            source_id="src-1",
            source_type="sidecar",
            source_label="Chrome",
            health="auth_failed",
            last_attempt_at=datetime.now(UTC),
        )
    )
    _seed_card(session, stale=True)

    client = TestClient(app)

    fleet_body = client.get("/api/v1/usage/fleet").json()
    (entry,) = [e for e in fleet_body["fleet"] if e["provider_id"] == PROVIDER]
    assert entry["critical_gauge"]["stale_reason"] == "credential_failing"

    limits_body = client.get("/api/v1/usage/limits").json()
    assert limits_body["limits"][0]["stale_reason"] == "credential_failing"


def test_non_stale_card_has_no_stale_reason(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    _seed_card(session, stale=False)

    client = TestClient(app)

    fleet_body = client.get("/api/v1/usage/fleet").json()
    (entry,) = [e for e in fleet_body["fleet"] if e["provider_id"] == PROVIDER]
    assert entry["critical_gauge"].get("stale_reason") is None

    limits_body = client.get("/api/v1/usage/limits").json()
    assert limits_body["limits"][0]["stale_reason"] is None
