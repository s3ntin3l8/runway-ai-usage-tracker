"""GitHub device-flow endpoints must be admin-gated.

Before this fix, `/api/v1/auth/github/{init,poll,logout}` had no auth
dependency at all — any caller who could reach the server could kick off a
device-flow login (that ends with this server persisting a GitHub access
token to disk) or wipe an existing one. `/status` stays open; it's a read.

Same pattern as `test_multi_account_provider_config.py::test_delete_provider_config_requires_admin_key`
and `test_status_reset.py::test_reset_provider_requires_admin_key`: patch
both `app.core.config.settings` and `app.core.security.settings` (the
latter is what `resolve_auth` actually reads), and bind off-loopback so the
localhost-trust bypass doesn't mask the assertion.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def off_loopback_admin(monkeypatch):
    for dotted in ("app.core.config.settings", "app.core.security.settings"):
        monkeypatch.setattr(f"{dotted}.ADMIN_API_KEY", "admin-secret")
        monkeypatch.setattr(f"{dotted}.APP_HOST", "0.0.0.0")


def test_init_device_flow_requires_admin_key(client, off_loopback_admin):
    response = client.get("/api/v1/auth/github/init")
    assert response.status_code == 403


def test_poll_device_flow_requires_admin_key(client, off_loopback_admin):
    response = client.post("/api/v1/auth/github/poll", json={"device_code": "does-not-matter"})
    assert response.status_code == 403


def test_logout_requires_admin_key(client, off_loopback_admin):
    with patch("app.api.endpoints.github_oauth.os.path.exists", return_value=True):
        response = client.post("/api/v1/auth/github/logout")
    assert response.status_code == 403


def test_status_stays_unauthenticated(client, off_loopback_admin):
    """`/status` is a read endpoint and stays open, same as every other
    unauthenticated `/usage/*` and `/system/status` GET."""
    with patch("app.api.endpoints.github_oauth.os.path.exists", return_value=False):
        response = client.get("/api/v1/auth/github/status")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_poll_success_does_not_log_access_token(client, caplog):
    """The full token-endpoint response (including the raw access token) must
    never reach the log at INFO — only failure/status detail is worth
    logging, and only at DEBUG."""
    from app.api.endpoints import github_oauth

    fake_data = {"access_token": "gho_super-secret-token", "token_type": "bearer"}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return fake_data

        status_code = 200

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **k):
            return _FakeResponse()

        async def get(self, *a, **k):
            return _FakeResponse()

    with (
        caplog.at_level(logging.DEBUG),
        patch("app.api.endpoints.github_oauth.httpx.AsyncClient", return_value=_FakeClient()),
        patch.object(github_oauth, "save_token", new_callable=AsyncMock),
    ):
        response = client.post("/api/v1/auth/github/poll", json={"device_code": "abc"})

    assert response.status_code == 200
    assert "gho_super-secret-token" not in caplog.text
