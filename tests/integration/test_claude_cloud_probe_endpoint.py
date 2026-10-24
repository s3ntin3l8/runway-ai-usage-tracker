"""The explicit diagnostic inherits admin/Host/CSRF gates and inert GETs."""

from unittest.mock import AsyncMock

import pytest

from app.api.endpoints import claude_cloud_probe as endpoint
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from tests.fixtures.test_client import TestClient

PATH = "/api/v1/system/debug/claude-cloud"
BODY = {"account_id": "holder@example.com", "source_id": "fixture"}


@pytest.fixture
def client(monkeypatch):
    limiter.reset()
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "probe-test-admin")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "")
    fake = AsyncMock(
        return_value={
            "outcome": "source_unavailable",
            "stage": "source",
            "sequence": {"numeric": 0, "invalid": 0, "duplicates": 0, "gaps": 0, "out_of_order": 0},
        }
    )
    monkeypatch.setattr(endpoint, "probe_cloud_history", fake)
    with TestClient(app, base_url="http://testserver") as client:
        yield client, fake
    limiter.reset()


def test_unauthenticated_post_and_get_are_inert(client):
    http, service = client
    assert http.post(PATH, json=BODY).status_code == 403
    assert http.get(PATH, headers={"X-Admin-Key": "probe-test-admin"}).status_code in (404, 405)
    service.assert_not_awaited()


def test_admin_post_validates_and_returns_only_sanitized_schema(client):
    http, service = client
    response = http.post(PATH, json=BODY, headers={"X-Admin-Key": "probe-test-admin"})
    assert response.status_code == 200
    assert response.json()["ready_for_ingestion"] is False
    assert response.json()["cloud_contract_verified"] is False
    assert "holder@example.com" not in response.text
    assert service.await_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"max_history_pages": True},
        {"max_history_pages": 4},
        {"session_id": "../../other?private"},
        {"oauth_token": "private-input"},
    ],
)
def test_invalid_input_is_rejected_without_echoing_sensitive_values(client, change):
    http, service = client
    response = http.post(PATH, json={**BODY, **change}, headers={"X-Admin-Key": "probe-test-admin"})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_probe_request"}
    assert "private-input" not in response.text
    service.assert_not_awaited()


def test_cross_origin_and_bad_host_cannot_start_probe(client):
    http, service = client
    response = http.post(
        PATH,
        json=BODY,
        headers={"X-Admin-Key": "probe-test-admin", "Origin": "https://evil.invalid"},
    )
    assert response.status_code == 403
    response = http.post(
        PATH, json=BODY, headers={"X-Admin-Key": "probe-test-admin", "Host": "evil.invalid"}
    )
    assert response.status_code in (400, 403)
    service.assert_not_awaited()


def test_large_body_is_bounded_before_validation(client):
    http, service = client
    response = http.post(
        PATH,
        content=b"x" * 9000,
        headers={"X-Admin-Key": "probe-test-admin", "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "probe_request_too_large"}
    service.assert_not_awaited()


def test_operator_rate_limit_prevents_additional_probe(client):
    http, service = client
    statuses = [
        http.post(PATH, json=BODY, headers={"X-Admin-Key": "probe-test-admin"}).status_code
        for _ in range(3)
    ]
    assert statuses == [200, 200, 429]
    assert service.await_count == 2


def test_duplicate_request_keys_cannot_select_a_different_source(client):
    http, service = client
    response = http.post(
        PATH,
        content=b'{"account_id":"holder@example.com","source_id":"one","source_id":"two"}',
        headers={"X-Admin-Key": "probe-test-admin", "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_probe_request"}
    service.assert_not_awaited()
