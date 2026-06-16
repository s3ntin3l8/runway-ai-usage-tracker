"""Integration tests for the session-cookie auth endpoints (issue #92/#100/#103)."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

import app.core.sessions as sessions
from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.db import AuditLog


@pytest.fixture
def client(monkeypatch):
    fd, path = tempfile.mkstemp()
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    # sessions.py reads/writes SESSION_SECRET via its own engine — point it at
    # the temp DB and reset the cached signer so the test is isolated.
    monkeypatch.setattr(sessions, "engine", engine)
    monkeypatch.setattr(sessions, "_signer", None)
    # Require auth, and bind non-localhost so the localhost bypass doesn't mask
    # the cookie path we're exercising.
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin-secret")
    monkeypatch.setattr(settings, "APP_HOST", "0.0.0.0")
    # The dev .env may set TLS_TERMINATED=1, which marks the cookie Secure so
    # the http TestClient won't echo it back. Force the plain-http case here.
    monkeypatch.setattr(settings, "TLS_TERMINATED", False)

    session = Session(engine)

    def _override():
        return session

    app.dependency_overrides[get_session] = _override
    yield TestClient(app), engine
    app.dependency_overrides.clear()
    session.close()
    engine.dispose()
    os.close(fd)
    if os.path.exists(path):
        os.remove(path)


def test_login_sets_cookie_and_authenticates(client):
    c, _ = client
    assert c.get("/api/v1/system/settings").json()["is_authenticated"] is False

    r = c.post("/api/v1/auth/session", json={"key": "admin-secret", "remember": False})
    assert r.status_code == 200
    assert r.json() == {"is_authenticated": True}

    set_cookie = r.headers.get("set-cookie", "").lower()
    assert "runway_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "max-age=43200" in set_cookie  # 12h default

    # The cookie is now in the client jar → the probe reports authenticated.
    assert c.get("/api/v1/system/settings").json()["is_authenticated"] is True


def test_remember_me_uses_longer_max_age(client):
    c, _ = client
    r = c.post("/api/v1/auth/session", json={"key": "admin-secret", "remember": True})
    assert "max-age=2592000" in r.headers.get("set-cookie", "").lower()  # 30d


def test_login_rejects_bad_key(client):
    c, _ = client
    r = c.post("/api/v1/auth/session", json={"key": "wrong"})
    assert r.status_code == 403
    assert "set-cookie" not in r.headers
    assert c.get("/api/v1/system/settings").json()["is_authenticated"] is False


def test_logout_clears_cookie(client):
    c, _ = client
    c.post("/api/v1/auth/session", json={"key": "admin-secret"})
    assert c.get("/api/v1/system/settings").json()["is_authenticated"] is True

    assert c.post("/api/v1/auth/logout").status_code == 204
    assert c.get("/api/v1/system/settings").json()["is_authenticated"] is False


def test_session_cookie_authorizes_mutation_and_records_audit(client):
    c, engine = client
    c.post("/api/v1/auth/session", json={"key": "admin-secret"})

    # A mutating admin endpoint accepts the cookie — no X-Admin-Key header.
    assert c.post("/api/v1/auth/revoke-all").status_code == 204

    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "auth.revoke_all")).all()
    assert rows, "revoke-all should write an audit row"
    assert rows[0].actor_type == "session"
    assert rows[0].actor == "session"


def test_revoke_all_invalidates_existing_sessions(client):
    c, _ = client
    c.post("/api/v1/auth/session", json={"key": "admin-secret"})
    assert c.get("/api/v1/system/settings").json()["is_authenticated"] is True

    assert c.post("/api/v1/auth/revoke-all").status_code == 204
    # Same cookie no longer verifies after the secret rotation.
    assert c.get("/api/v1/system/settings").json()["is_authenticated"] is False


def test_admin_key_header_still_works(client):
    """Backwards-compat: API/script clients keep using X-Admin-Key."""
    c, _ = client
    r = c.get("/api/v1/system/settings", headers={"X-Admin-Key": "admin-secret"})
    assert r.json()["is_authenticated"] is True
