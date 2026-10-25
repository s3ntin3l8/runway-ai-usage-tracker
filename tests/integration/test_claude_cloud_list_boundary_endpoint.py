"""Only explicit authenticated POSTs can request a failed-page diagnostic."""

import importlib
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from tests.fixtures.test_client import TestClient

PATH = "/api/v1/system/debug/claude-cloud-list-boundary"
AUTH = {"X-Admin-Key": "boundary-test-admin"}
BODY = {"job_handle": "a" * 32}


@pytest.fixture
def client(monkeypatch):
    endpoint = importlib.import_module("app.api.endpoints.claude_cloud_list_boundary")
    from app.services.claude_cloud_list_boundary import _empty

    limiter.reset()
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "boundary-test-admin")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "")
    service = AsyncMock(return_value=_empty())
    monkeypatch.setattr(endpoint, "inspect_list_boundary", service)
    with TestClient(app, base_url="http://testserver") as http:
        yield http, service
    limiter.reset()


def test_only_explicit_admin_post_dispatches(client):
    http, service = client
    assert http.get(PATH, headers=AUTH).status_code in (404, 405)
    assert http.post(PATH, json=BODY).status_code == 403
    service.assert_not_awaited()
    response = http.post(PATH, json=BODY, headers=AUTH)
    assert response.status_code == 200 and response.json()["requests"] == 0
    service.assert_awaited_once()


@pytest.mark.parametrize(
    "body",
    [
        '{"job_handle":"private-value"}',
        '{"job_handle":"a","job_handle":"b"}',
        '{"job_handle":"' + "a" * 32 + '","cursor":"private-value"}',
        "x" * 9000,
    ],
    ids=["invalid-handle", "duplicate-key", "extra-value", "oversized"],
)
def test_invalid_inputs_never_echo_values(client, body):
    http, service = client
    response = http.post(PATH, content=body, headers=AUTH)
    assert response.status_code in (413, 422)
    assert "private-value" not in response.text
    service.assert_not_awaited()


def test_host_csrf_and_rate_guards_are_preserved(client):
    http, service = client
    assert http.post(PATH, json=BODY, headers={**AUTH, "Host": "attacker.invalid"}).status_code in (
        400,
        403,
    )
    assert (
        http.post(
            PATH, json=BODY, headers={**AUTH, "Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    service.assert_not_awaited()
    assert [http.post(PATH, json=BODY, headers=AUTH).status_code for _ in range(3)] == [
        200,
        200,
        429,
    ]
    assert service.await_count == 2


def test_invalid_service_data_has_fixed_error_without_values(client, caplog):
    http, service = client
    service.return_value = {"private-field": "private-value"}
    response = http.post(PATH, json=BODY, headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "invalid_boundary_report"}
    assert "private-value" not in response.text + caplog.text
