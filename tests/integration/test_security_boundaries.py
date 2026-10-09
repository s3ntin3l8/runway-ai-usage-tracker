"""Regression coverage for the security audit's HTTP boundary exploits."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.parametrize("path", ["/api/v1/system/settings", "/api/v1/usage/limits"])
def test_loopback_rejects_rebound_host(monkeypatch, path):
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "127.0.0.1")
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    response = client.get(path, headers={"Host": "rebind.attacker.test:8765"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Untrusted Host"


def test_hostile_origin_cannot_revoke_local_sessions(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "127.0.0.1")
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    response = client.post("/api/v1/auth/revoke-all", headers={"Origin": "https://attacker.test"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Untrusted Origin"


def test_local_vite_origin_matches_forwarded_host(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "127.0.0.1")
    client = TestClient(app, base_url="http://127.0.0.1:5173")
    response = client.get("/api/v1/system/settings", headers={"Origin": "http://127.0.0.1:5173"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/usage/limits",
        "/api/v1/usage/events",
        "/api/v1/fleet/sidecars",
        "/api/v1/fleet/sidecars/laptop",
        "/api/v1/system/provider-configs",
        "/api/v1/system/status",
        "/api/v1/system/dashboard-layout",
        "/api/v1/system/app-config",
        "/api/v1/auth/github/status",
    ],
)
def test_network_private_reads_require_auth(monkeypatch, path):
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "0.0.0.0")
    monkeypatch.setattr("app.core.security.settings.ADMIN_API_KEY", "test-admin")
    assert TestClient(app).get(path).status_code == 403


def test_network_layout_write_requires_auth(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "0.0.0.0")
    monkeypatch.setattr("app.core.security.settings.ADMIN_API_KEY", "test-admin")
    assert TestClient(app).put("/api/v1/system/dashboard-layout", json={}).status_code == 403


def test_network_auth_bootstrap_remains_public(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "0.0.0.0")
    monkeypatch.setattr("app.core.security.settings.ADMIN_API_KEY", "test-admin")
    client = TestClient(app)
    assert client.get("/api/v1/system/settings").status_code == 200
    assert client.get("/api/v1/system/health").status_code == 200


def test_remote_dev_proxy_cannot_inherit_loopback_admin(monkeypatch):
    rotate_secret = Mock()
    audit_record = Mock()
    monkeypatch.setattr("app.api.endpoints.auth.rotate_secret", rotate_secret)
    monkeypatch.setattr("app.api.endpoints.auth.audit_log.record", audit_record)
    for module in ("app.core.config", "app.core.security"):
        monkeypatch.setattr(f"{module}.settings.APP_HOST", "127.0.0.1")
        monkeypatch.setattr(f"{module}.settings.ADMIN_API_KEY", "dev-admin")
    client = TestClient(app, base_url="http://127.0.0.1:5173", client=("127.0.0.1", 12345))
    headers = {"X-Runway-Dev-Remote": "1"}
    response = client.get("/api/v1/system/settings", headers=headers)
    assert response.status_code == 200
    assert response.json()["is_authenticated"] is False
    assert client.post("/api/v1/auth/revoke-all", headers=headers).status_code == 403
    rotate_secret.assert_not_called()
    audit_record.assert_not_called()
    headers["X-Admin-Key"] = "dev-admin"
    assert client.post("/api/v1/auth/revoke-all", headers=headers).status_code == 204
    rotate_secret.assert_called_once_with()
    audit_record.assert_called_once()


def test_remote_dev_proxy_cannot_forge_sso(monkeypatch):
    for module in ("app.core.config", "app.core.security"):
        monkeypatch.setattr(f"{module}.settings.APP_HOST", "127.0.0.1")
        monkeypatch.setattr(f"{module}.settings.ADMIN_API_KEY", "dev-admin")
        monkeypatch.setattr(f"{module}.settings.TRUSTED_PROXY_IPS", "127.0.0.1")
    client = TestClient(app, base_url="http://127.0.0.1:5173", client=("127.0.0.1", 12345))
    headers = {"X-Runway-Dev-Remote": "1", "X-Forwarded-User": "forged-admin"}
    assert client.post("/api/v1/auth/revoke-all", headers=headers).status_code == 403


def test_production_host_policy_rejects_test_client_alias(monkeypatch):
    from app.core.net import LOOPBACK_HOSTS

    monkeypatch.setattr("app.core.http_security.local_request_hosts", lambda: LOOPBACK_HOSTS)
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "127.0.0.1")
    assert TestClient(app).get("/api/v1/system/settings").status_code == 403
