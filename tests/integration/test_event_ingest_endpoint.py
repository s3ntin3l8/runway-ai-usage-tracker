"""Integration test: POST /api/v1/fleet/ingest accepts events[] and deduplicates."""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import SidecarRegistry, UsageEvent
from app.services.pricing_seed import seed_pricing_table

TEST_KEY = "test-ingest-key-phase3"


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        seed_pricing_table(s)
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


def _signed(payload: dict, key: str = TEST_KEY) -> tuple[bytes, dict]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(time.time())
    sig = hmac.new(key.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
    return body, {"X-Signature": sig, "X-Timestamp": ts}


def test_ingest_events_inserts_rows_and_dedups(session):
    payload = {
        "provider": "anthropic-sidecar",
        "sidecar_id": "dev-01",
        "metrics": [],
        "events": [
            {
                "provider_id": "anthropic",
                "account_id": "u@x",
                "event_id": "msg_a",
                "ts": "2026-05-08T14:00:00+00:00",
                "model_id": "sonnet",
                "tokens_input": 100,
                "tokens_output": 50,
            },
            {
                "provider_id": "anthropic",
                "account_id": "u@x",
                "event_id": "msg_b",
                "ts": "2026-05-08T14:01:00+00:00",
                "model_id": "sonnet",
                "tokens_input": 200,
                "tokens_output": 100,
            },
        ],
    }

    with (
        patch("app.api.endpoints.fleet.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)

        body, headers = _signed(payload)
        r = client.post("/api/v1/fleet/ingest", content=body, headers=headers)

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["events_inserted"] == 2
    assert data["events_duplicate"] == 0

    # Replay the same payload — both events should be deduped
    with (
        patch("app.api.endpoints.fleet.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)

        body2, headers2 = _signed(payload)
        r2 = client.post("/api/v1/fleet/ingest", content=body2, headers=headers2)

    assert r2.status_code == 200, r2.text
    assert r2.json()["events_duplicate"] == 2
    assert r2.json()["events_inserted"] == 0

    rows = session.exec(select(UsageEvent)).all()
    assert len(rows) == 2


def test_ingest_normalizes_sidecar_id(session):
    """An FQDN/`.local`-flapping host collapses onto one normalized sidecar id."""
    payload = {
        "provider": "anthropic-sidecar",
        "sidecar_id": "Macbook.in.s3ntin3l8.de",
        "sidecar_version": "2.3.0",
        "metrics": [],
        "events": [
            {
                "provider_id": "anthropic",
                "account_id": "u@x",
                "event_id": "msg_norm",
                "ts": "2026-05-08T14:00:00+00:00",
                "model_id": "sonnet",
                "tokens_input": 100,
                "tokens_output": 50,
            },
        ],
    }

    with (
        patch("app.api.endpoints.fleet.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)

        body, headers = _signed(payload)
        r = client.post("/api/v1/fleet/ingest", content=body, headers=headers)

    assert r.status_code == 200, r.text

    # Both the event row and the registry entry are keyed off the normalized id.
    ev = session.exec(select(UsageEvent).where(UsageEvent.event_id == "msg_norm")).one()
    assert ev.sidecar_id == "macbook"
    reg_ids = session.exec(select(SidecarRegistry.sidecar_id)).all()
    assert reg_ids == ["macbook"]
