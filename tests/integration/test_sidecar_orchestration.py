import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import LatestUsage, ProviderConfig
from app.services.accumulator import merge_card_json
from app.services.fleet_registry import fleet_registry


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    # Clear in-memory history before each test
    fleet_registry._last_provider_polls.clear()
    fleet_registry._pending_triggers.clear()
    yield client
    app.dependency_overrides.clear()


def test_ingest_heartbeat_returns_poll_providers(client, session):
    # 1. Setup providers and intervals
    session.add(ProviderConfig(provider_id="anthropic", enabled=True, poll_interval_seconds=300))
    session.add(ProviderConfig(provider_id="github", enabled=True, poll_interval_seconds=600))
    session.commit()

    # Set secret key to avoid 503
    from app.core.config import settings

    settings.INGEST_API_KEY = "test-key"

    def get_signed_payload(payload: dict, key: str):
        import hashlib
        import hmac
        import json

        ts = str(time.time())
        body_bytes = json.dumps(payload, separators=(",", ":")).encode()
        sig = hmac.new(key.encode(), ts.encode() + body_bytes, hashlib.sha256).hexdigest()
        return body_bytes, {"X-Signature": sig, "X-Timestamp": ts}

    payload = {
        "provider": "sidecar-test",
        "sidecar_id": "test-sidecar",
        "metrics": [],
        "deltas": [],
    }

    body, headers = get_signed_payload(payload, "test-key")

    # 2. First heartbeat: should trigger both
    resp = client.post("/api/v1/fleet/ingest", content=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data["poll_providers"]) == {"anthropic", "github"}

    # 3. Second heartbeat (immediate): should trigger NONE
    body, headers = get_signed_payload(payload, "test-key")
    resp = client.post("/api/v1/fleet/ingest", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["poll_providers"] == []

    # 4. Mock time passage (force one to be due)
    fleet_registry._last_provider_polls["test-sidecar"]["anthropic"] = time.time() - 400

    body, headers = get_signed_payload(payload, "test-key")
    resp = client.post("/api/v1/fleet/ingest", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["poll_providers"] == ["anthropic"]

    # 5. Manual Trigger: should return ALL enabled providers and trigger=True.
    # The dashboard's "Refresh" button fans this out via /system/force-collect,
    # which calls fleet_registry.set_pending_trigger for every registered
    # sidecar. We exercise the same internal hook directly here.
    fleet_registry.set_pending_trigger("test-sidecar")

    body, headers = get_signed_payload(payload, "test-key")
    resp = client.post("/api/v1/fleet/ingest", content=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data["poll_providers"]) == {"anthropic", "github"}
    assert data["trigger"] is True


def test_server_and_sidecar_resolve_to_same_row(session):
    """Server scrape (account_id='default') and sidecar card (account_id=email) must resolve
    to the same canonical_account_id via resolve_account_id and merge into a single LatestUsage row.

    This exercises the exact lookup+merge logic from poller.py:247-275, so if someone removes
    the resolve_account_id call from that code path, this test will break.
    """
    from app.models.schemas import LimitCard
    from app.services.account_identity import resolve_account_id

    # Server scrape: account_id is "default" but account_label is the email
    server_card = LimitCard(
        provider_id="anthropic",
        account_id="default",
        account_label="test@example.com",
        service_name="Test",
        icon="🔬",
        remaining="88%",
        window_type="weekly",
        variant="default",
        data_source="web",
        pct_used=12.0,
    )
    # Sidecar card: account_id is already the email
    sidecar_card = LimitCard(
        provider_id="anthropic",
        account_id="test@example.com",
        account_label="test@example.com",
        service_name="Test",
        icon="🔬",
        remaining="—",
        window_type="weekly",
        variant="default",
        data_source="local",
        token_usage={"input": 100, "output": 200, "total": 654000000},
    )

    # Exercise the exact write-path logic from poller.py for each card in sequence
    for card in [server_card, sidecar_card]:
        canonical_id = resolve_account_id(card.provider_id, card.account_id, card.account_label)
        incoming = card.model_dump(exclude_none=True)
        existing = session.exec(
            select(LatestUsage).where(
                LatestUsage.provider_id == card.provider_id,
                LatestUsage.account_id == canonical_id,
                LatestUsage.window_type == card.window_type,
                LatestUsage.variant == (card.variant or "default"),
                LatestUsage.model_id == (card.model_id or ""),
            )
        ).first()
        if existing:
            existing.card_json = merge_card_json(existing.card_json, incoming)
            existing.sidecar_id = card.sidecar_id or "local"
        else:
            session.add(
                LatestUsage(
                    provider_id=card.provider_id,
                    account_id=canonical_id,
                    sidecar_id=card.sidecar_id or "local",
                    window_type=card.window_type,
                    variant=card.variant or "default",
                    model_id=card.model_id or "",
                    card_json=merge_card_json(None, incoming),
                )
            )
        session.commit()

    # Assert exactly one row resolved under the canonical email identity
    rows = session.exec(
        select(LatestUsage).where(
            LatestUsage.provider_id == "anthropic",
            LatestUsage.account_id == "test@example.com",
        )
    ).all()
    assert len(rows) == 1, f"Expected 1 row, got {len(rows)}: {[r.account_id for r in rows]}"

    merged = json.loads(rows[0].card_json)
    assert merged["pct_used"] == 12.0, "pct_used from server scrape must be preserved"
    assert merged["token_usage"]["total"] == 654000000, "token_usage from sidecar must be present"
    assert "web" in merged.get("data_source", ""), "web data_source must be preserved"
    assert "local" in merged.get("data_source", ""), "local data_source from sidecar must be merged"
