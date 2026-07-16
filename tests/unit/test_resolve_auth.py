"""Unit tests for the shared auth resolver (app/core/security.py)."""

from unittest.mock import MagicMock

from fastapi import Request

import app.core.security as security
from app.core.config import settings


def _req(host: str = "1.2.3.4", headers: dict[str, str] | None = None) -> Request:
    req = MagicMock(spec=Request)
    req.client = MagicMock()
    req.client.host = host
    req.headers = headers or {}
    return req


def _resolve(req: Request, **kw):
    base = {
        "x_admin_key": None,
        "session_cookie": None,
    }
    base.update(kw)
    return security.resolve_auth(req, **base)


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
    req = _req(
        "10.0.0.5",
        headers={
            "X-Forwarded-User": "alice",
            "X-Forwarded-Email": "a@b.c",
            "X-Forwarded-Groups": "admins",
        },
    )
    res = _resolve(req)
    assert res.authenticated
    assert res.actor_type == "proxy"
    assert res.actor_id == "alice"
    assert res.actor == "alice"
    assert res.actor_meta == {"email": "a@b.c", "groups": "admins"}


def test_proxy_header_ignored_from_untrusted_ip(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.5")
    res = _resolve(_req("9.9.9.9", headers={"X-Forwarded-User": "alice"}))
    assert not res.authenticated


def test_remote_user_fallback_header(monkeypatch):
    """Back-compat: Remote-User is still read when the configured header is absent."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.5")
    res = _resolve(_req("10.0.0.5", headers={"Remote-User": "bob"}))
    assert res.authenticated
    assert res.actor_type == "proxy"
    assert res.actor_id == "bob"


def test_configurable_header_names_support_authentik(monkeypatch):
    """Pointing FORWARD_AUTH_*_HEADER at Authentik's outpost headers works
    with no proxy-side header renaming."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.5")
    monkeypatch.setattr(settings, "FORWARD_AUTH_USER_HEADER", "X-authentik-username")
    monkeypatch.setattr(settings, "FORWARD_AUTH_EMAIL_HEADER", "X-authentik-email")
    monkeypatch.setattr(settings, "FORWARD_AUTH_GROUPS_HEADER", "X-authentik-groups")
    req = _req(
        "10.0.0.5",
        headers={
            "X-authentik-username": "carol",
            "X-authentik-email": "carol@example.com",
            "X-authentik-groups": "runway-admins|everyone",
        },
    )
    res = _resolve(req)
    assert res.authenticated
    assert res.actor_type == "proxy"
    assert res.actor_id == "carol"
    assert res.actor_meta == {"email": "carol@example.com", "groups": "runway-admins|everyone"}


def test_allowed_groups_match_grants_proxy_trust(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.5")
    monkeypatch.setattr(settings, "FORWARD_AUTH_ALLOWED_GROUPS", "runway-admins")
    req = _req(
        "10.0.0.5",
        headers={"X-Forwarded-User": "carol", "X-Forwarded-Groups": "everyone|runway-admins"},
    )
    res = _resolve(req)
    assert res.authenticated
    assert res.actor_type == "proxy"


def test_allowed_groups_mismatch_falls_through_to_denied(monkeypatch):
    """A proxy user who isn't in the configured allowlist doesn't get the
    proxy branch — with nothing else to fall back to, the request is denied."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.5")
    monkeypatch.setattr(settings, "FORWARD_AUTH_ALLOWED_GROUPS", "runway-admins")
    req = _req(
        "10.0.0.5",
        headers={"X-Forwarded-User": "mallory", "X-Forwarded-Groups": "everyone"},
    )
    res = _resolve(req)
    assert not res.authenticated


def test_allowed_users_match_grants_proxy_trust(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.5")
    monkeypatch.setattr(settings, "FORWARD_AUTH_ALLOWED_USERS", "alice, carol")
    req = _req("10.0.0.5", headers={"X-Forwarded-User": "carol"})
    res = _resolve(req)
    assert res.authenticated
    assert res.actor_type == "proxy"


def test_unauthorized_proxy_user_falls_through_to_session_cookie(monkeypatch):
    """A break-glass path: an unauthorized proxy user doesn't lock out a real
    admin login — the session cookie (or api key) is still evaluated."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.5")
    monkeypatch.setattr(settings, "FORWARD_AUTH_ALLOWED_GROUPS", "runway-admins")
    monkeypatch.setattr(security, "verify_session", lambda t: t == "good")
    req = _req(
        "10.0.0.5",
        headers={"X-Forwarded-User": "mallory", "X-Forwarded-Groups": "everyone"},
    )
    res = _resolve(req, session_cookie="good")
    assert res.authenticated
    assert res.actor_type == "session"


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
