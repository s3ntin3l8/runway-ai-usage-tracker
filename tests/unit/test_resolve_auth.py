"""Unit tests for the shared auth resolver (app/core/security.py)."""

from unittest.mock import MagicMock

from fastapi import Request

import app.core.security as security
from app.core.config import settings
from app.core.security import resolve_auth


def _req(host: str = "1.2.3.4", headers: dict[str, str] | None = None) -> Request:
    req = MagicMock(spec=Request)
    req.client = MagicMock()
    req.client.host = host
    req.headers = headers or {}
    return req


def _resolve(req: Request, **kw):
    base = {
        "x_admin_key": None,
        "x_forwarded_user": None,
        "remote_user": None,
        "session_cookie": None,
    }
    base.update(kw)
    return resolve_auth(req, **base)


def test_no_admin_key_is_open(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", None)
    res = _resolve(_req())
    assert res.authenticated
    assert res.actor_type == "none"
    # Legacy display string preserved for audit-log readers.
    assert res.actor == "no-admin-key-configured"


def test_localhost_trust(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "127.0.0.1")
    res = _resolve(_req("127.0.0.1"))
    assert res.authenticated
    assert res.actor_type == "localhost"


def test_proxy_trust_captures_identity(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.5")
    req = _req("10.0.0.5", headers={"X-Forwarded-Email": "a@b.c", "X-Forwarded-Groups": "admins"})
    res = _resolve(req, x_forwarded_user="alice")
    assert res.authenticated
    assert res.actor_type == "proxy"
    assert res.actor_id == "alice"
    assert res.actor == "alice"
    assert res.actor_meta == {"email": "a@b.c", "groups": "admins"}


def test_proxy_header_ignored_from_untrusted_ip(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.5")
    res = _resolve(_req("9.9.9.9"), x_forwarded_user="alice")
    assert not res.authenticated


def test_session_cookie_branch(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    # Stub verify_session so the branch is exercised without touching the DB.
    monkeypatch.setattr(security, "verify_session", lambda t: t == "good")
    ok = _resolve(_req("8.8.8.8"), session_cookie="good")
    assert ok.authenticated
    assert ok.actor_type == "session"
    assert not _resolve(_req("8.8.8.8"), session_cookie="bad").authenticated


def test_api_key_header(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "secret")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    ok = _resolve(_req("8.8.8.8"), x_admin_key="secret")
    assert ok.authenticated
    assert ok.actor_type == "api-key"
    bad = _resolve(_req("8.8.8.8"), x_admin_key="nope")
    assert not bad.authenticated
    assert bad.actor_type == "none"
