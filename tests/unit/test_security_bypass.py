from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request

from app.core.config import settings
from app.core.security import require_admin_key


@pytest.mark.asyncio
async def test_require_admin_key_local_trust():
    # Mock settings
    settings.ADMIN_API_KEY = "test-key"
    settings.APP_HOST = "127.0.0.1"

    # Mock request from localhost
    request = MagicMock(spec=Request)
    request.client = MagicMock()
    request.client.host = "127.0.0.1"

    # Should not raise exception
    await require_admin_key(request, x_admin_key=None, session_cookie=None)


@pytest.mark.asyncio
async def test_require_admin_key_proxy_trust(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "test-key")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "192.168.1.1")

    request = MagicMock(spec=Request)
    request.client = MagicMock()
    request.client.host = "192.168.1.1"
    request.headers = {"X-Forwarded-User": "user123"}

    # Should not raise exception when the trusted proxy asserts a user.
    await require_admin_key(request, x_admin_key=None, session_cookie=None)

    # The CGI-style Remote-User fallback also works.
    request.headers = {"Remote-User": "user123"}
    await require_admin_key(request, x_admin_key=None, session_cookie=None)


@pytest.mark.asyncio
async def test_require_admin_key_standard_fail(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "test-key")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")  # Remote bound

    request = MagicMock(spec=Request)
    request.client = MagicMock()
    request.client.host = "192.168.1.1"
    request.headers = {}

    # Should raise 403
    with pytest.raises(HTTPException) as excinfo:
        await require_admin_key(request, x_admin_key="wrong-key", session_cookie=None)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_empty_admin_key_is_not_a_valid_credential(monkeypatch):
    # Config normalizes a blank key to None; defend the gate anyway. An empty
    # ADMIN_API_KEY must read as "no key configured" (open) — never let an empty
    # X-Admin-Key header authenticate as the api-key actor.
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")  # not localhost-trusted

    request = MagicMock(spec=Request)
    request.client = MagicMock()
    request.client.host = "203.0.113.5"

    # Must not raise, and must NOT be attributed as a valid api-key login.
    await require_admin_key(request, x_admin_key="", session_cookie=None)
    assert request.state.admin_actor == "no-admin-key-configured"
