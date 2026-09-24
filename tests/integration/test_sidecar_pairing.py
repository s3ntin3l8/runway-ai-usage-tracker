"""Sidecar pairing: admin mints a one-time code, a new sidecar redeems it once.

See app/services/pairing.py and docs/SECURITY.md → *Sidecar pairing*.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import app.core.config as app_config
from app.core.db import get_session
from app.main import app
from app.models.db import AuditLog, SidecarPairingCode
from app.services import pairing

INGEST_KEY = "k" * 40  # pragma: allowlist secret


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="settings")
def settings_fixture():
    # Resolve at call time: other suites reload app.core.config, and the
    # endpoints read the module attribute, not an import-time binding.
    return app_config.settings


@pytest.fixture(name="client")
def client_fixture(session, monkeypatch, settings):
    monkeypatch.setattr(settings, "INGEST_API_KEY", INGEST_KEY)
    monkeypatch.setattr(settings, "PUBLIC_URL", "")
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _mint(client, server_url="https://runway.example.com") -> dict:
    r = client.post("/api/v1/fleet/pairing-codes", json={"server_url": server_url})
    assert r.status_code == 200, r.text
    return r.json()


def _actions(session) -> list[str]:
    return [a.action for a in session.exec(select(AuditLog).order_by(AuditLog.ts)).all()]


class TestMint:
    def test_returns_code_and_deep_link(self, client, session):
        body = _mint(client)
        assert len(pairing.normalize(body["code"])) == 10
        assert body["server_url"] == "https://runway.example.com"
        link = urlsplit(body["deep_link"])
        assert (link.scheme, link.netloc) == ("runway-sidecar", "pair")
        q = parse_qs(link.query)
        assert q == {"server": ["https://runway.example.com"], "code": [body["code"]]}
        # Only the hash is stored; the code never lands in the audit log.
        row = session.exec(select(SidecarPairingCode)).one()
        assert row.code_hash == pairing.hash_code(body["code"])
        assert body["code"] not in str(session.exec(select(AuditLog)).all())
        assert _actions(session) == ["sidecar.pairing_code.create"]

    def test_public_url_wins_over_browser_origin(self, client, settings, monkeypatch):
        monkeypatch.setattr(settings, "PUBLIC_URL", "https://pinned.example.com/")
        assert _mint(client, "http://lan-ip:8765")["server_url"] == "https://pinned.example.com"

    def test_falls_back_to_request_base_url(self, client):
        r = client.post("/api/v1/fleet/pairing-codes")
        assert r.status_code == 200
        assert r.json()["server_url"] == "http://testserver"

    @pytest.mark.parametrize(
        "bad", ["javascript:alert(1)", "ftp://x", "https://user:pw@x", "https://x/?a=b"]
    )
    def test_rejects_unsafe_server_url_and_falls_back(self, client, bad):
        assert _mint(client, bad)["server_url"] == "http://testserver"

    def test_requires_admin(self, client, settings, monkeypatch):
        import app.core.security as security

        # require_admin_key reads the settings object bound in app.core.security.
        monkeypatch.setattr(
            security.settings, "ADMIN_API_KEY", "admin-secret"
        )  # pragma: allowlist secret
        assert client.post("/api/v1/fleet/pairing-codes", json={}).status_code == 403
        ok = client.post(
            "/api/v1/fleet/pairing-codes",
            json={},
            headers={"X-Admin-Key": "admin-secret"},  # pragma: allowlist secret
        )
        assert ok.status_code == 200

    @pytest.mark.parametrize("key", ["", "sidecar-default-secret"])
    def test_refuses_while_ingest_disabled(self, client, settings, monkeypatch, key):
        monkeypatch.setattr(settings, "INGEST_API_KEY", key)
        assert client.post("/api/v1/fleet/pairing-codes", json={}).status_code == 503


class TestRedeem:
    def test_redeem_once(self, client, session):
        code = _mint(client)["code"]
        r = client.post("/api/v1/fleet/pair", json={"code": code, "hostname": "Laptop.local"})
        assert r.status_code == 200
        assert r.json() == {"api_url": "https://runway.example.com", "api_key": INGEST_KEY}
        row = session.exec(select(SidecarPairingCode)).one()
        assert row.used_at is not None
        assert row.used_by_hostname == "laptop"

        again = client.post("/api/v1/fleet/pair", json={"code": code})
        assert again.status_code == 400
        assert _actions(session) == [
            "sidecar.pairing_code.create",
            "sidecar.pair",
            "sidecar.pair.rejected",
        ]

    def test_hand_typed_code_is_normalized(self, client):
        code = _mint(client)["code"]
        typed = code.lower().replace("-", " ").replace("0", "o").replace("1", "l")
        assert client.post("/api/v1/fleet/pair", json={"code": typed}).status_code == 200

    def test_expired_code_rejected(self, client, session):
        code = _mint(client)["code"]
        row = session.exec(select(SidecarPairingCode)).one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(row)
        session.commit()
        assert client.post("/api/v1/fleet/pair", json={"code": code}).status_code == 400

    @pytest.mark.parametrize("code", ["", "ABCDE-FGHJK", "short", "x" * 500])
    def test_unknown_code_rejected_uniformly(self, client, code):
        _mint(client)
        r = client.post("/api/v1/fleet/pair", json={"code": code})
        assert r.status_code == 400
        assert r.json()["detail"] == "Invalid or expired pairing code"

    def test_redeem_refused_while_ingest_disabled(self, client, settings, monkeypatch):
        code = _mint(client)["code"]
        monkeypatch.setattr(settings, "INGEST_API_KEY", "")
        assert client.post("/api/v1/fleet/pair", json={"code": code}).status_code == 503

    def test_redeem_is_rate_limited(self, client):
        statuses = [
            client.post("/api/v1/fleet/pair", json={"code": "ABCDE-FGHJK"}).status_code
            for _ in range(12)
        ]
        assert 429 in statuses

    def test_old_rows_swept_on_mint(self, client, session):
        session.add(
            SidecarPairingCode(
                code_hash="old",
                server_url="https://x",
                expires_at=datetime.now(UTC) - timedelta(days=3),
            )
        )
        session.commit()
        _mint(client)
        hashes = [r.code_hash for r in session.exec(select(SidecarPairingCode)).all()]
        assert "old" not in hashes and len(hashes) == 1


class TestCodeFormat:
    def test_codes_are_random_and_crockford(self):
        codes = {pairing.generate_code() for _ in range(200)}
        assert len(codes) == 200
        for c in codes:
            assert len(c) == 11 and c[5] == "-"
            assert not set(c.replace("-", "")) & set("ILOU")
