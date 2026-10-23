"""Integration test: MiniMax Code runtime events land on the MiniMax card.

The sidecar's MiniMax Code extractor pushes one event per API request under
``provider_id="minimax"`` with ``source_app="minimax_code"``. This test
runs the real extractor against a runtime-shaped store and pushes the
result through the fleet ingest endpoint, so the whole path — attribution,
pricing and the ``(provider_id, event_id)`` idempotency — is covered.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import UsageEvent
from app.services.pricing_seed import seed_pricing_table
from scripts.sidecar_pkg.event_extractors.minimax_code import parse_minimax_code_events
from scripts.sidecar_pkg.transport import signing_timestamp
from tests.fixtures.minimax_code_fixture import DEFAULT_USAGE, make_minimax_code_store
from tests.fixtures.test_client import TestClient

TEST_KEY = "test-minimax-code-ingest-key"


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
    ts = signing_timestamp()
    sig = hmac.new(key.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
    return body, {"X-Signature": sig, "X-Timestamp": ts}


def _push(events: list[dict]) -> dict:
    payload = {
        "provider": "minimax",
        "sidecar_id": "dev-01",
        "metrics": [],
        "events": events,
    }
    with (
        patch("app.core.config.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)
        body, headers = _signed(payload)
        response = client.post("/api/v1/fleet/ingest", content=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _extracted(tmp_path) -> list[dict]:
    db_path = make_minimax_code_store(tmp_path)
    events = parse_minimax_code_events(
        db_path,
        account_id="minimax-user@example.com",
        # Derived from the fixture's own timestamps, not the wall clock: a
        # fixed "now minus N days" would fall out of the bootstrap window
        # as soon as the fixture grew older than the window.
        since=datetime.fromtimestamp(DEFAULT_USAGE[0]["ts"] / 1000, tz=UTC) - timedelta(days=1),
        state_file=tmp_path / "minimax_code_watermark.json",
    )
    payloads = []
    for event in events:
        payload = event.model_dump(mode="json")
        # The sidecar's events branch stamps the attribution decision it
        # made; a tagged account is the resolved path.
        payload["account_source"] = "tag"
        payloads.append(payload)
    return payloads


def test_minimax_code_events_land_under_the_minimax_provider(session, tmp_path):
    events = _extracted(tmp_path)
    assert events

    data = _push(events)
    assert data["events_inserted"] == len(events)
    assert data["events_duplicate"] == 0

    rows = list(session.exec(select(UsageEvent).where(UsageEvent.provider_id == "minimax")).all())
    assert len(rows) == len(events)
    for row in rows:
        assert row.source_app == "minimax_code"
        assert row.account_id == "minimax-user@example.com"
    # The session whose telemetry named a model keeps it; the request whose
    # message row never landed is still stored, just without a model.
    models = {row.model_id for row in rows}
    assert "MiniMax-M3.1-Flash" in models


def test_minimax_code_events_are_priced_from_the_minimax_rate(session, tmp_path):
    events = _extracted(tmp_path)
    _push(events)

    rows = list(session.exec(select(UsageEvent).where(UsageEvent.provider_id == "minimax")).all())
    priced = [row for row in rows if row.cost_usd]
    assert priced
    first = next(row for row in priced if row.tokens_input == 16474)
    # MiniMax-M3.1-Flash: 0.15 / 0.60 / 0.015 USD per Mtok.
    expected = (
        round(16474 / 1_000_000 * 0.15, 6)
        + round(203 / 1_000_000 * 0.60, 6)
        + round(1536 / 1_000_000 * 0.015, 6)
    )
    assert first.cost_usd == pytest.approx(expected, abs=1e-6)


def test_replaying_the_same_minimax_code_events_is_a_no_op(session, tmp_path):
    events = _extracted(tmp_path)
    _push(events)
    before = len(session.exec(select(UsageEvent)).all())

    data = _push(events)
    assert data["events_inserted"] == 0
    assert data["events_duplicate"] == len(events)
    assert len(session.exec(select(UsageEvent)).all()) == before
