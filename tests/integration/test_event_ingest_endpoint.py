"""Integration test: POST /api/v1/fleet/ingest accepts events[] and deduplicates."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import CredentialSource, PendingUsageEvent, SidecarRegistry, UsageEvent
from app.services.pricing_seed import seed_pricing_table
from scripts.sidecar_pkg.transport import signing_timestamp
from tests.fixtures.test_client import TestClient

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
    ts = signing_timestamp()
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
        patch("app.core.config.settings") as mock_settings,
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
        patch("app.core.config.settings") as mock_settings,
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
        patch("app.core.config.settings") as mock_settings,
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


def test_replay_cannot_repeat_state_updates_but_fresh_retry_works(session, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.INGEST_API_KEY", TEST_KEY)
    client = TestClient(app)
    payload = {
        "provider": "sidecar-replay",
        "sidecar_id": "replay-host",
        "metrics": [],
        "events": [],
    }
    body, headers = _signed(payload)
    assert client.post("/api/v1/fleet/ingest", content=body, headers=headers).status_code == 200
    row = session.get(SidecarRegistry, "replay-host")
    session.refresh(row)
    first_count = row.ingest_count
    response = client.post("/api/v1/fleet/ingest", content=body, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "replayed_request"
    session.refresh(row)
    assert row.ingest_count == first_count
    body, headers = _signed(payload)
    assert client.post("/api/v1/fleet/ingest", content=body, headers=headers).status_code == 200
    session.refresh(row)
    assert row.ingest_count == first_count + 1


def test_handler_failure_consumes_signature_and_fresh_retry_is_accepted(session, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.INGEST_API_KEY", TEST_KEY)
    client = TestClient(app)
    body, headers = _signed({"invalid": "payload"})
    assert client.post("/api/v1/fleet/ingest", content=body, headers=headers).status_code == 400
    assert client.post("/api/v1/fleet/ingest", content=body, headers=headers).status_code == 409
    body, headers = _signed({"invalid": "payload"})
    assert client.post("/api/v1/fleet/ingest", content=body, headers=headers).status_code == 400


def test_a_hermes_event_is_resolved_from_the_hosts_own_native_identity(session):
    """A Hermes event names the provider, never the account.

    The host's native collector for that provider *does* know the account —
    it reported the credential, and Runway filed it under the account the
    credential resolved to. That answer is used so the event does not sit in
    "Unassigned usage" until the operator tags it by hand (#931).
    """
    session.add(
        CredentialSource(
            provider_id="anthropic",
            account_id="me@example.com",
            source_id="sidecar:dev-01:file:~/.claude/.credentials.json",
            source_type="sidecar",
            source_label="credentials.json",
            credential_origin="file:~/.claude/.credentials.json",
            sidecar_id="dev-01",
        )
    )
    session.commit()

    payload = {
        "provider": "anthropic-sidecar",
        "sidecar_id": "dev-01",
        "metrics": [],
        "events": [
            {
                "provider_id": "anthropic",
                "account_id": "default",
                "account_source": "default",
                "event_id": "hermes|profile|session|sonnet|task|c1s1",
                "ts": "2026-10-24T10:00:00+00:00",
                "model_id": "sonnet",
                "tokens_input": 100,
                "tokens_output": 50,
            },
        ],
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
        r = client.post("/api/v1/fleet/ingest", content=body, headers=headers)

    assert r.status_code == 200, r.text
    assert session.exec(select(PendingUsageEvent)).all() == []
    row = session.exec(select(UsageEvent)).one()
    assert row.account_id == "me@example.com"
    assert row.attribution_source == "local"


def _post_ingest(payload: dict):
    with (
        patch("app.core.config.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)
        body, headers = _signed(payload)
        return client.post("/api/v1/fleet/ingest", content=body, headers=headers)


def _names_payload(name: str = "Fake session title") -> dict:
    return {
        "provider": "anthropic-sidecar",
        "sidecar_id": "dev-01",
        "metrics": [],
        "events": [
            {
                "provider_id": "anthropic",
                "account_id": "u@x",
                "event_id": "msg_n1",
                "ts": "2026-05-08T14:00:00+00:00",
                "session_id": "sess-names-1",
                "tokens_input": 10,
            }
        ],
        "session_names": [
            {
                "provider_id": "anthropic",
                "session_id": "sess-names-1",
                "source_app": "claude_code",
                "name": name,
                "kind": "generated",
            }
        ],
    }


def test_ingest_stores_session_names_and_replay_is_a_noop(session, caplog):
    from app.models.db import SessionName

    fake_title = "Confidential wording zz9"
    with caplog.at_level("DEBUG"):
        r = _post_ingest(_names_payload(fake_title))
        assert r.status_code == 200, r.text
        assert r.json()["names_upserted"] == 1
        assert r.json()["names_error"] is False
        assert r.json()["events_inserted"] == 1
        r2 = _post_ingest(_names_payload(fake_title))
    assert r2.json()["names_upserted"] == 0
    assert r2.json()["names_error"] is False
    rows = session.exec(select(SessionName)).all()
    assert [(x.session_id, x.name, x.kind) for x in rows] == [
        ("sess-names-1", fake_title, "generated")
    ]
    assert "Confidential" not in caplog.text


def test_ingest_without_session_names_reports_zero(session):
    payload = _names_payload()
    del payload["session_names"]
    r = _post_ingest(payload)
    assert r.status_code == 200, r.text
    assert r.json()["names_upserted"] == 0
    assert r.json()["names_error"] is False


def test_failing_name_upsert_does_not_fail_events(session, caplog):
    fake_title = "Confidential wording zz9"
    with (
        caplog.at_level("DEBUG"),
        patch(
            "app.services.session_names.upsert_session_names",
            side_effect=RuntimeError("boom"),
        ),
    ):
        r = _post_ingest(_names_payload(fake_title))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["events_inserted"] == 1
    assert data["events_error"] is False
    assert data["names_upserted"] == 0
    assert data["names_error"] is True
    assert "Confidential" not in caplog.text
    assert len(session.exec(select(UsageEvent)).all()) == 1


def test_bad_name_entry_does_not_reject_the_payload(session):
    payload = _names_payload()
    payload["session_names"][0]["kind"] = "bogus"
    r = _post_ingest(payload)
    assert r.status_code == 200, r.text
    assert r.json()["events_inserted"] == 1
    assert r.json()["names_upserted"] == 0
