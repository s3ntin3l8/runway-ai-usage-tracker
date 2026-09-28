"""Multi-host deployment safety gates.

These tests cover the audit's S3 / S4 / S5 findings, plus the newer
ADMIN_API_KEY-or-TRUSTED_PROXY_IPS gate:

* Refuse to start when bound to a non-localhost interface without an
  explicit TLS termination assertion. Sidecar payloads carry OAuth tokens
  and cookies; HMAC protects integrity but not confidentiality.
* Refuse to start when bound to a non-localhost interface without an
  explicit CORS_ORIGINS env var. The legacy fallback to `["*"]` combined
  with `allow_credentials=True` is rejected by every browser.
* Refuse to start when bound to a non-localhost interface with neither
  ADMIN_API_KEY nor TRUSTED_PROXY_IPS set — `resolve_auth` would otherwise
  treat every caller as admin.
* Standard security headers (CSP, X-Content-Type-Options, Referrer-Policy)
  on every response.

Every "rejects" test below sets an ADMIN_API_KEY so it exercises only the
one precondition it names — the ADMIN_API_KEY-or-proxy gate has its own
tests further down.
"""

from __future__ import annotations

import pytest

from app.core.config import _validate_security_invariants, settings


def _restore(monkeypatch, **kwargs):
    """Apply attribute overrides to the live settings via monkeypatch."""
    for k, v in kwargs.items():
        monkeypatch.setattr(settings, k, v, raising=False)


def test_validator_passes_for_localhost_defaults(monkeypatch):
    """The default localhost bind never triggers production gates."""
    _restore(
        monkeypatch,
        APP_HOST="127.0.0.1",
        ADMIN_API_KEY=None,
        DB_ENCRYPTION_KEY=None,
        TLS_TERMINATED=False,
    )
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    _validate_security_invariants(settings)  # no raise


def test_validator_rejects_non_localhost_without_db_encryption(monkeypatch):
    """Existing rail — kept here so a refactor can't quietly drop it."""
    _restore(
        monkeypatch,
        APP_HOST="0.0.0.0",
        ADMIN_API_KEY="admin-secret",  # pragma: allowlist secret
        DB_ENCRYPTION_KEY=None,
        TLS_TERMINATED=True,
    )
    monkeypatch.setenv("CORS_ORIGINS", "https://runway.example.com")
    with pytest.raises(RuntimeError, match="DB_ENCRYPTION_KEY"):
        _validate_security_invariants(settings)


def test_validator_rejects_non_localhost_without_tls_termination(monkeypatch):
    """Sidecar→server traffic carries tokens; HMAC isn't enough on its own.

    Operators behind a TLS-terminating proxy must opt in via
    RUNWAY_TLS_TERMINATED=1 so the server can refuse to start when nobody's
    actually terminating TLS.
    """
    _restore(
        monkeypatch,
        APP_HOST="0.0.0.0",
        ADMIN_API_KEY="admin-secret",  # pragma: allowlist secret
        DB_ENCRYPTION_KEY="fernet-key-placeholder",
        TLS_TERMINATED=False,
    )
    monkeypatch.setenv("CORS_ORIGINS", "https://runway.example.com")
    with pytest.raises(RuntimeError, match="TLS"):
        _validate_security_invariants(settings)


def test_validator_rejects_non_localhost_without_explicit_cors_origins(monkeypatch):
    """The `["*"]` fallback combined with allow_credentials=True is invalid
    per the CORS spec — and silently broken in browsers. Refuse to start
    rather than ship a non-functional config."""
    _restore(
        monkeypatch,
        APP_HOST="0.0.0.0",
        ADMIN_API_KEY="admin-secret",  # pragma: allowlist secret
        DB_ENCRYPTION_KEY="fernet-key-placeholder",
        TLS_TERMINATED=True,
    )
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        _validate_security_invariants(settings)


def test_validator_accepts_fully_configured_non_localhost(monkeypatch):
    _restore(
        monkeypatch,
        APP_HOST="0.0.0.0",
        ADMIN_API_KEY="admin-secret",  # pragma: allowlist secret
        DB_ENCRYPTION_KEY="fernet-key-placeholder",
        TLS_TERMINATED=True,
    )
    monkeypatch.setenv("CORS_ORIGINS", "https://runway.example.com")
    _validate_security_invariants(settings)  # no raise


def test_validator_rejects_non_localhost_with_no_key_and_no_proxy(monkeypatch):
    """The new S1 gate: on a non-localhost bind, `resolve_auth`'s
    "no ADMIN_API_KEY configured" branch would otherwise treat every caller
    as admin. Refuse to start unless there's a key to check, or a forward
    proxy is the intended gate instead."""
    _restore(
        monkeypatch,
        APP_HOST="0.0.0.0",
        ADMIN_API_KEY=None,
        TRUSTED_PROXY_IPS="",
        DB_ENCRYPTION_KEY="fernet-key-placeholder",
        TLS_TERMINATED=True,
    )
    monkeypatch.setenv("CORS_ORIGINS", "https://runway.example.com")
    with pytest.raises(RuntimeError, match="ADMIN_API_KEY"):
        _validate_security_invariants(settings)


def test_validator_accepts_non_localhost_with_trusted_proxy_and_no_key(monkeypatch):
    """A forward-auth deployment doesn't need its own ADMIN_API_KEY — the
    proxy is the gate, per docs/SECURITY.md and docs/forward-auth.md."""
    _restore(
        monkeypatch,
        APP_HOST="0.0.0.0",
        ADMIN_API_KEY=None,
        TRUSTED_PROXY_IPS="10.0.0.5",
        DB_ENCRYPTION_KEY="fernet-key-placeholder",
        TLS_TERMINATED=True,
    )
    monkeypatch.setenv("CORS_ORIGINS", "https://runway.example.com")
    _validate_security_invariants(settings)  # no raise


def test_security_headers_present_on_dashboard(monkeypatch):
    """Every response carries the defence-in-depth header set."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/v1/system/health")

    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    # frame-ancestors 'none' supersedes X-Frame-Options in modern browsers;
    # both are set for backwards compatibility with older user agents.
    assert resp.headers.get("X-Frame-Options") == "DENY"
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
