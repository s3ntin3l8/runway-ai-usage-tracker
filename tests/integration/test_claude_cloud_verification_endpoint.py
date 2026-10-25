"""Explicit verification actions inherit admin, Host and CSRF protections."""

import importlib
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from tests.fixtures.test_client import TestClient

PATH = "/api/v1/system/debug/claude-cloud-verification"
AUTH = {"X-Admin-Key": "verification-test-admin"}
HANDLE = "a" * 32


@pytest.fixture
def client(monkeypatch):
    endpoint = importlib.import_module("app.api.endpoints.claude_cloud_verification")
    limiter.reset()
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "verification-test-admin")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "")
    advance = AsyncMock(side_effect=AssertionError("unexpected_provider_access"))
    monkeypatch.setattr(endpoint, "advance_verification", advance)
    with TestClient(app, base_url="http://testserver") as http:
        yield http, endpoint, advance
    limiter.reset()


def test_get_is_inert_and_unauthenticated_post_is_denied(client):
    http, _, advance = client
    assert http.get(PATH, headers=AUTH).status_code in (404, 405)
    assert http.post(PATH, json={"action": "step", "job_handle": HANDLE}).status_code == 403
    advance.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        '{"action":"status","secret":"private-fixture"}',  # pragma: allowlist secret - synthetic privacy fixture
        '{"action":"status","action":"step"}',
        '{"action":"step","job_handle":"private-fixture"}',
        '{"action":"status","value":' + '"' + "x" * (256 * 1024) + '"}',
    ],
    ids=["extra-field", "duplicate-key", "invalid-handle", "oversized"],
)
def test_invalid_inputs_never_echo_values_or_dispatch(client, body):
    http, _, advance = client
    response = http.post(PATH, content=body, headers=AUTH)
    assert response.status_code in (413, 422)
    assert "private-fixture" not in response.text
    assert "xxx" not in response.text
    advance.assert_not_awaited()


def test_status_and_cancel_use_only_checkpoint_storage(client, monkeypatch):
    from types import SimpleNamespace

    http, endpoint, advance = client
    calls = []
    store = SimpleNamespace(
        current_handle=lambda: None,
        cancel=lambda handle: calls.append(handle),
    )
    monkeypatch.setattr(endpoint, "checkpoint_store", lambda: store)
    response = http.post(PATH, json={"action": "status"}, headers=AUTH)
    assert response.status_code == 200 and response.json() == {"state": "absent"}
    response = http.post(PATH, json={"action": "cancel", "job_handle": HANDLE}, headers=AUTH)
    assert response.status_code == 200 and response.json() == {"state": "cancelled"}
    assert calls == [HANDLE]
    advance.assert_not_awaited()


def test_checkpoint_errors_are_fixed_and_do_not_dispatch(client, monkeypatch):
    from app.services.claude_cloud_checkpoints import CheckpointError

    http, endpoint, advance = client

    def unavailable():
        raise CheckpointError("private-fixture")

    monkeypatch.setattr(endpoint, "checkpoint_store", unavailable)
    response = http.post(PATH, json={"action": "status"}, headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "checkpoint_unavailable"}
    advance.assert_not_awaited()


def test_status_returns_committed_projection_without_advancing(client, monkeypatch):
    from types import SimpleNamespace

    from app.models.claude_cloud_verification import VerificationState

    http, endpoint, advance = client
    state = VerificationState(
        account_id="private-holder",
        source_id="private-source",
        job_key="a" * 64,
        source_digest="b" * 64,
        created_at=1000.0,
        deadline=2800.0,
    )
    store = SimpleNamespace(current_handle=lambda: HANDLE, snapshot=lambda handle: state)
    monkeypatch.setattr(endpoint, "checkpoint_store", lambda: store)
    response = http.post(PATH, json={"action": "status"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["job_handle"] == HANDLE
    assert response.json()["requests"] == 0
    assert "private-" not in response.text
    assert state.job_key not in response.text
    advance.assert_not_awaited()


def test_bad_host_never_dispatches(client):
    http, _, advance = client
    response = http.post(
        PATH,
        json={"action": "step", "job_handle": HANDLE},
        headers={**AUTH, "Host": "attacker.invalid"},
    )
    assert response.status_code in (400, 403)
    advance.assert_not_awaited()


def test_seventh_action_is_limited_before_provider_dispatch(client, monkeypatch):
    from types import SimpleNamespace

    http, endpoint, advance = client
    monkeypatch.setattr(
        endpoint, "checkpoint_store", lambda: SimpleNamespace(current_handle=lambda: None)
    )
    for _ in range(6):
        assert http.post(PATH, json={"action": "status"}, headers=AUTH).status_code == 200
    assert http.post(PATH, json={"action": "status"}, headers=AUTH).status_code == 429
    advance.assert_not_awaited()


def test_cross_origin_cannot_advance(client):
    http, _, advance = client
    response = http.post(
        PATH,
        json={"action": "step", "job_handle": HANDLE},
        headers={**AUTH, "Origin": "https://attacker.invalid"},
    )
    assert response.status_code == 403
    advance.assert_not_awaited()


def test_large_local_fixture_fits_new_limit_without_changing_old_endpoint(client, monkeypatch):
    http, endpoint, advance = client
    start = AsyncMock(side_effect=endpoint.CheckpointError("job_exists"))
    monkeypatch.setattr(endpoint, "start_verification", start)
    record = {
        "type": "assistant",
        "message": {
            "id": "a" * 64,
            "usage": {
                "input_tokens": 1,
                "output_tokens": 0,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 3,
            },
        },
    }
    response = http.post(
        PATH,
        json={
            "action": "start",
            "account_id": "fixture",
            "source_id": "fixture",
            "job_key": "b" * 64,
            "expected_local": [record] * 50,
        },
        headers=AUTH,
    )
    assert response.status_code == 409 and response.json() == {"detail": "job_exists"}
    start.assert_awaited_once()
    advance.assert_not_awaited()


def test_invalid_service_output_is_a_fixed_error_without_logging_values(
    client, monkeypatch, caplog
):
    from types import SimpleNamespace

    http, endpoint, _ = client
    monkeypatch.setattr(endpoint, "checkpoint_store", lambda: SimpleNamespace())
    monkeypatch.setattr(endpoint, "read_verification", AsyncMock(return_value=None))
    monkeypatch.setattr(
        endpoint, "build_verification_report", lambda *args: {"unexpected": "private-fixture"}
    )
    response = http.post(PATH, json={"action": "status", "job_handle": HANDLE}, headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "invalid_verification_report"}
    assert "private-fixture" not in response.text + caplog.text


def test_all_256_context_rich_local_records_fit_the_route_budget(client, monkeypatch):
    http, endpoint, _ = client
    start = AsyncMock(side_effect=endpoint.CheckpointError("job_exists"))
    monkeypatch.setattr(endpoint, "start_verification", start)
    record = {
        "type": "assistant",
        "session_scope": "a" * 64,
        "request_scope": "b" * 64,
        "parent_tool_use_id": "c" * 64,
        "record_version": "2.1.45",
        "sdk_timestamp": 12345.0,
        "provider_timestamp": 12346.0,
        "message": {
            "id": "d" * 64,
            "usage": {
                "input_tokens": 123456789,
                "output_tokens": 123456789,
                "cache_read_input_tokens": 123456789,
                "cache_creation_input_tokens": 123456789,
            },
        },
    }
    response = http.post(
        PATH,
        json={
            "action": "start",
            "account_id": "fixture",
            "source_id": "fixture",
            "job_key": "e" * 64,
            "expected_local": [record] * 256,
        },
        headers=AUTH,
    )
    assert response.status_code == 409 and response.json() == {"detail": "job_exists"}
    start.assert_awaited_once()


def test_status_recovers_handle_for_unreadable_checkpoint_and_cancel_stays_explicit(
    client, monkeypatch
):
    from types import SimpleNamespace

    http, endpoint, advance = client
    cancelled = []
    store = SimpleNamespace(current_handle=lambda: HANDLE, cancel=lambda h: cancelled.append(h))
    monkeypatch.setattr(endpoint, "checkpoint_store", lambda: store)
    monkeypatch.setattr(
        endpoint,
        "read_verification",
        AsyncMock(side_effect=endpoint.CheckpointError("checkpoint_unavailable")),
    )
    response = http.post(PATH, json={"action": "status"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"state": "unavailable", "job_handle": HANDLE}
    assert cancelled == []
    response = http.post(
        PATH, json={"action": "cancel", "job_handle": response.json()["job_handle"]}, headers=AUTH
    )
    assert response.status_code == 200 and cancelled == [HANDLE]
    advance.assert_not_awaited()


@pytest.mark.parametrize("failure", ["key_rotated", "key_disabled", "tampered"])
def test_unreadable_real_checkpoint_can_be_discovered_and_explicitly_cancelled(
    client, monkeypatch, tmp_path, failure
):
    import sqlite3

    from cryptography.fernet import Fernet

    from app.core.encryption import EncryptionService
    from tests.unit.test_claude_cloud_checkpoints import setup_store

    http, endpoint, advance = client
    store, state, path, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    if failure == "key_rotated":
        store.crypto = EncryptionService(Fernet.generate_key().decode())
    elif failure == "key_disabled":
        store.crypto = EncryptionService("")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE claude_cloud_verification SET state_encrypted=?", ("broken",)
            )
    monkeypatch.setattr(endpoint, "checkpoint_store", lambda: store)
    response = http.post(PATH, json={"action": "status"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"state": "unavailable", "job_handle": handle}
    assert store.current_handle() == handle
    assert "private-" not in response.text
    cancelled = http.post(PATH, json={"action": "cancel", "job_handle": handle}, headers=AUTH)
    assert cancelled.status_code == 200
    assert store.current_handle() is None
    advance.assert_not_awaited()


def test_terminal_step_rechecks_local_authority_before_publishing_cached_evidence(
    client, monkeypatch
):
    from types import SimpleNamespace

    from app.models.claude_cloud_verification import VerificationState

    http, endpoint, advance = client
    terminal = VerificationState(
        account_id="private-revoked-holder",
        source_id="private-source",
        job_key="a" * 64,
        source_digest="b" * 64,
        created_at=1000.0,
        deadline=2800.0,
        phase="complete",
        outcome="complete",
        requests=3,
        list_pages=1,
        listed_sessions=1,
        seen_sessions=["c" * 64],
        selected_session="fixture-session",
        list_complete=True,
        history_pages=1,
        history_complete=True,
        subject="22222222-2222-4222-8222-222222222222",
        organization="11111111-1111-4111-8111-111111111111",
    )
    monkeypatch.setattr(
        endpoint, "checkpoint_store", lambda: SimpleNamespace(snapshot=lambda handle: terminal)
    )
    advance.side_effect = None
    advance.return_value = terminal
    # The real local-only reader finds no live source for this revoked account.
    response = http.post(PATH, json={"action": "step", "job_handle": HANDLE}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["outcome"] == "source_changed"
    assert response.json()["account_verified"] is False
    assert terminal.outcome == "complete"  # publication cannot mutate a concurrent checkpoint
    advance.assert_awaited_once()
