"""Full device ingest and withdrawal with synthetic credentials; no native IO."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core import config, db
from app.core.db import get_session
from app.core.encryption import EncryptionService
from app.main import app
from app.models.db import SidecarSourceContribution
from app.services import sidecar_devices, source_controls
from app.services.collector_manager import manager
from app.services.token_cache import TokenCache
from scripts.sidecar_pkg.source_catalog import catalog
from scripts.sidecar_pkg.source_protocol import signed_headers
from scripts.sidecar_pkg.transport import signing_timestamp
from tests.fixtures.test_client import TestClient


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    encryption = EncryptionService(Fernet.generate_key().decode())
    monkeypatch.setattr(sidecar_devices, "encryption_service", encryption)
    monkeypatch.setattr(source_controls, "encryption_service", encryption)
    monkeypatch.setattr(config.settings, "INGEST_API_KEY", "synthetic-shared")
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(manager, "reconcile_token_cache_from_durable_tags", AsyncMock())
    with Session(engine) as session:
        app.dependency_overrides[get_session] = lambda: session
        device, secret = sidecar_devices.enroll(session, "synthetic")
        session.commit()
        local = {
            "api_url": "https://runway.example",
            "device_api_url": "https://runway.example",
            "api_key": "synthetic-shared",
            "device_id": device.device_id,
            "device_key": secret,
        }
        monkeypatch.setattr(
            source_controls,
            "read_snapshot",
            lambda previous, *_: (
                source_controls.snapshot(session)
                if source_controls.revision(session) != previous
                else None
            ),
        )
        yield TestClient(app), session, local
        app.dependency_overrides.pop(get_session, None)
    engine.dispose()


def post(client, local, path, purpose, value):
    body = json.dumps(value).encode()
    headers = signed_headers(
        local, local["api_url"] + path, local["api_key"], signing_timestamp(), purpose, body
    )
    return client.post(path, content=body, headers=headers)


def report(read=True, generation=1):
    source = catalog()["anthropic.claude-code"]
    return {
        "source_id": source.id,
        "provider_id": source.provider,
        "label": source.label,
        "revision": source.revision,
        "generation": generation,
        "read": read,
        "state": "Limited",
    }


@pytest.mark.asyncio
async def test_ingest_then_off_rejects_delayed_credentials_and_keeps_refresh_local(setup):
    client, session, local = setup
    path = "/api/v1/fleet/ingest"
    assert (
        post(
            client, local, "/api/v1/fleet/source-sync", "source-sync", {"sources": [report()]}
        ).status_code
        == 200
    )
    proof = {key: report()[key] for key in ("source_id", "revision", "generation")}
    card = {
        "provider_id": "anthropic",
        "service_name": "Claude Code",
        "remaining": "Token",
        "unit": "oauth",
        "metadata": {
            "provider_id": "anthropic",
            "credential_origin": "keychain:Claude Code-credentials",
            "oauth_token": "synthetic-access",
            "refresh_token": "synthetic-local-refresh",
            "oauth_read_only": "1",
            "keychain_consent": proof,
        },
    }
    payload = {
        "provider": "synthetic-sidecar",
        "sidecar_id": "forged-other-host",
        "metrics": [card],
        "events": [],
    }
    response = post(client, local, path, "ingest", payload)
    assert response.status_code == 200, response.text
    row = session.exec(select(SidecarSourceContribution)).one()
    assert row.device_id == local["device_id"]
    decrypted = source_controls.snapshot(session)[1][0]
    assert "refresh_token" not in decrypted["tokens"]
    cache = TokenCache()
    assert len(await cache.get_pending_sources("anthropic")) == 1
    assert (
        post(
            client,
            local,
            "/api/v1/fleet/source-sync",
            "source-sync",
            {"sources": [report(False, 2)]},
        ).status_code
        == 200
    )
    assert post(client, local, path, "ingest", payload).status_code == 200
    assert await cache.get_pending_sources("anthropic") == []
    assert session.exec(select(SidecarSourceContribution)).one().payload_encrypted is None


@pytest.mark.parametrize("platform", ["Linux", "Windows", "Darwin", None])
def test_mixed_upload_keeps_files_and_platform_eligible_cookies(setup, platform):
    from app.models.db import SidecarDevice

    client, session, local = setup
    device = session.get(SidecarDevice, local["device_id"])
    device.platform = platform
    session.add(device)
    session.commit()

    def card(origin):
        return {
            "provider_id": "anthropic",
            "service_name": "Synthetic",
            "remaining": "Token",
            "unit": "oauth",
            "metadata": {"credential_origin": origin, "oauth_token": "synthetic-access"},
        }

    origins = [
        "keychain:Some New Store",
        "cookie:anthropic/Chrome/Default/Cookies",
        "cookie:openai/Chrome/Default/Cookies",
        "file:synthetic-auth.json",
    ]
    response = post(
        client,
        local,
        "/api/v1/fleet/ingest",
        "ingest",
        {
            "provider": "synthetic",
            "metrics": [card(origin) for origin in origins],
            "os_platform": "Linux/forged",  # Ingest cannot alter the paired platform.
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["credentials_rejected"] == (2 if platform in {"Linux", "Windows"} else 3)
    stored = session.exec(select(SidecarSourceContribution)).all()
    assert {row.origin for row in stored} == set(
        [origins[1], origins[3]] if platform in {"Linux", "Windows"} else origins[3:]
    )
    assert {row.scraper_id for row in stored} == (
        {None, "anthropic.chrome"} if platform in {"Linux", "Windows"} else {None}
    )


def test_manifest_skips_unknown_store_without_claiming_completed_absence(setup):
    client, session, local = setup
    payload = {
        "sidecar_id": "synthetic",
        "entries": [
            {"provider_id": "anthropic", "credential_origin": "keychain:Future Store"},
            {"provider_id": "openai", "credential_origin": "file:synthetic-key.json"},
        ],
        "completed_providers": ["anthropic", "openai"],
    }
    response = post(client, local, "/api/v1/fleet/credentials/manifest", "manifest", payload)
    assert response.status_code == 200, response.text
    from app.models.db import PendingCredentialTag

    rows = session.exec(select(PendingCredentialTag)).all()
    assert {row.credential_origin for row in rows} == {"file:synthetic-key.json"}


def test_legacy_key_cannot_claim_reserved_device_registry(setup):
    client, session, local = setup
    legacy = {"api_url": local["api_url"], "api_key": local["api_key"]}
    response = post(
        client,
        legacy,
        "/api/v1/fleet/ingest",
        "ingest",
        {
            "provider": "synthetic",
            "sidecar_id": sidecar_devices.registry_id(local["device_id"]),
            "metrics": [],
        },
    )
    assert response.status_code == 403


def test_configured_engine_can_write_after_durable_auth_receipt(tmp_path, monkeypatch):
    from app.core import replay

    engine = create_engine(
        "sqlite:///" + str(tmp_path / "production.db"),
        connect_args={"check_same_thread": False, "timeout": 0.1},
    )
    db.configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    encryption = EncryptionService(Fernet.generate_key().decode())
    monkeypatch.setattr(sidecar_devices, "encryption_service", encryption)
    monkeypatch.setattr(source_controls, "encryption_service", encryption)
    monkeypatch.setattr(config.settings, "INGEST_API_KEY", "synthetic-shared")
    monkeypatch.setattr(replay, "receipt_engine", lambda: engine)

    def request_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = request_session
    try:
        with Session(engine) as session:
            device, secret = sidecar_devices.enroll(session, "synthetic", platform="Darwin")
            device_id = device.device_id
            session.commit()
        local = {
            "api_url": "https://runway.example",
            "device_api_url": "https://runway.example",
            "api_key": "synthetic-shared",
            "device_id": device_id,
            "device_key": secret,
        }
        response = post(
            TestClient(app),
            local,
            "/api/v1/fleet/source-sync",
            "source-sync",
            {"sources": [report()]},
        )
        assert response.status_code == 200, response.text
        with Session(engine) as session:
            assert source_controls.state(session, device_id, report()["source_id"]).local_read
    finally:
        app.dependency_overrides.pop(get_session, None)
        engine.dispose()


@pytest.mark.parametrize("reported", [False, True])
def test_nonmac_cookie_lifecycle_keeps_mapping_and_rejects_replay_after_revoke(setup, reported):
    from app.models.db import SidecarDevice

    client, session, local = setup
    device = session.get(SidecarDevice, local["device_id"])
    device.platform = "Linux"
    session.add(device)
    session.commit()
    if reported:
        source = catalog()["anthropic.chrome"]
        source_controls.apply_report(
            session,
            device.device_id,
            {**report(), "source_id": source.id, "revision": source.revision},
        )
        session.commit()
    cookie = {
        "provider_id": "anthropic",
        "service_name": "Synthetic",
        "remaining": "Token",
        "unit": "cookie",
        "metadata": {
            "credential_origin": "cookie:anthropic/Chrome/Default/Cookies",
            "cookie_sessionKey": "synthetic-cookie",
        },
    }
    payload = {"provider": "synthetic", "metrics": [cookie]}
    assert post(client, local, "/api/v1/fleet/ingest", "ingest", payload).status_code == 200
    row = session.exec(select(SidecarSourceContribution)).one()
    assert row.scraper_id == "anthropic.chrome"
    path = (
        "/api/v1/fleet/sidecars/"
        + sidecar_devices.registry_id(local["device_id"])
        + "/sources/anthropic.chrome/commands"
    )
    assert client.post(path, json={"action": "pause"}).status_code == 200
    assert source_controls.snapshot(session)[1] == []
    resumed = client.post(path, json={"action": "resume"})
    assert resumed.status_code == 200
    ack = post(
        client,
        local,
        "/api/v1/fleet/source-sync",
        "source-sync",
        {"acknowledgments": [{"id": resumed.json()["id"], "status": "applied"}]},
    )
    assert ack.status_code == 200
    assert len(source_controls.snapshot(session)[1]) == 1
    assert client.post(path, json={"action": "revoke"}).status_code == 200
    assert session.get(SidecarSourceContribution, row.id).payload_encrypted is None
    response = post(client, local, "/api/v1/fleet/ingest", "ingest", payload)
    assert response.status_code == 200 and response.json()["credentials_rejected"] == 1
    assert source_controls.snapshot(session)[1] == []
    # Even non-macOS uploads cannot downgrade an explicitly supplied bad proof.
    cookie["metadata"]["keychain_consent"] = {"source_id": "unknown"}
    response = post(client, local, "/api/v1/fleet/ingest", "ingest", payload)
    assert response.json()["credentials_rejected"] == 1


@pytest.mark.parametrize("platform", ["Windows", "Darwin", "Linux", None])
@pytest.mark.parametrize(
    "provider,origin",
    [
        ("github", "keychain:github.com"),
        ("antigravity", "keychain:gemini:antigravity"),
    ],
)
def test_known_windows_credentials_keep_existing_collection_without_mac_consent(
    setup, platform, provider, origin
):
    from app.models.db import SidecarDevice

    client, session, local = setup
    device = session.get(SidecarDevice, local["device_id"])
    device.platform = platform
    session.add(device)
    session.commit()
    response = post(
        client,
        local,
        "/api/v1/fleet/ingest",
        "ingest",
        {
            "provider": "synthetic",
            "metrics": [
                {
                    "provider_id": provider,
                    "service_name": "Synthetic",
                    "remaining": "Token",
                    "unit": "oauth",
                    "metadata": {
                        "credential_origin": origin,
                        "oauth_token": "synthetic-access",
                    },
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["credentials_rejected"] == (0 if platform == "Windows" else 1)
    rows = session.exec(select(SidecarSourceContribution)).all()
    assert len(rows) == (1 if platform == "Windows" else 0)
    if rows:
        assert rows[0].origin == origin
        assert rows[0].provider_id == provider
        assert rows[0].payload_encrypted
        source_id = provider + ".windows-credential"
        assert rows[0].scraper_id == source_id
        path = "/api/v1/fleet/sidecars/" + sidecar_devices.registry_id(local["device_id"])
        listed = client.get(path + "/sources")
        assert listed.status_code == 200
        assert any(row["source_id"] == source_id for row in listed.json()["sources"])
        endpoint = path + "/sources/" + source_id + "/commands"
        assert client.post(endpoint, json={"action": "pause"}).status_code == 200
        assert source_controls.snapshot(session)[1] == []
        resumed = client.post(endpoint, json={"action": "resume"})
        assert resumed.status_code == 200
        assert (
            post(
                client,
                local,
                "/api/v1/fleet/source-sync",
                "source-sync",
                {"acknowledgments": [{"id": resumed.json()["id"], "status": "applied"}]},
            ).status_code
            == 200
        )
        assert len(source_controls.snapshot(session)[1]) == 1
        assert client.post(endpoint, json={"action": "revoke"}).status_code == 200
        assert session.get(SidecarSourceContribution, rows[0].id).payload_encrypted is None
        assert source_controls.snapshot(session)[1] == []
        assert client.post(endpoint, json={"action": "resume"}).status_code == 409


@pytest.mark.parametrize(
    "provider,origin",
    [("github", "keychain:github.com"), ("antigravity", "keychain:gemini:antigravity")],
)
@pytest.mark.asyncio
async def test_windows_idle_heartbeat_keeps_source_freshness_separate_from_collection(
    setup, tmp_path, monkeypatch, provider, origin
):
    from app.models.db import CredentialSource, SidecarDevice, SidecarRegistry
    from scripts.sidecar_pkg import source_settings
    from scripts.sidecar_pkg.source_protocol import SourceClient

    client, session, local = setup
    device = session.get(SidecarDevice, local["device_id"])
    device.platform = "Windows"
    session.add(device)
    session.commit()
    credential_payload = {
        "provider": "synthetic",
        "metrics": [
            {
                "provider_id": provider,
                "service_name": "Synthetic",
                "remaining": "Token",
                "unit": "oauth",
                "metadata": {"credential_origin": origin, "oauth_token": "synthetic-access"},
            }
        ],
    }
    response = post(client, local, "/api/v1/fleet/ingest", "ingest", credential_payload)
    assert response.status_code == 200 and response.json()["credentials_rejected"] == 0
    contribution = session.exec(select(SidecarSourceContribution)).one()
    source_id = provider + ".windows-credential"
    registry_id = sidecar_devices.registry_id(local["device_id"])
    old_report = datetime.now(UTC) - timedelta(days=1)
    source = source_controls.state(session, local["device_id"], source_id)
    source.last_report_at = old_report
    session.add(source)
    registry = session.get(SidecarRegistry, registry_id)
    registry.last_seen = old_report
    session.add(registry)
    # Model successful server collection independently of local credential uploads.
    success = datetime.now(UTC) - timedelta(minutes=1)
    health = session.exec(
        select(CredentialSource).where(
            CredentialSource.source_id == contribution.credential_source_id
        )
    ).one()
    health.health = "healthy"
    health.last_success_at = success
    session.add(health)
    session.commit()
    monkeypatch.setattr(source_settings, "sys", SimpleNamespace(platform="win32"))
    source_client = SourceClient(local, tmp_path / "source-state.json")
    payload = source_client.payload(local)
    assert payload["sources"] == []  # Windows lifecycle sources have no local consent reports.
    assert (
        post(
            client,
            local,
            "/api/v1/fleet/ingest",
            "ingest",
            {"provider": "synthetic", "metrics": []},
        ).status_code
        == 200
    )
    sync = post(client, local, "/api/v1/fleet/source-sync", "source-sync", payload)
    assert sync.status_code == 200
    source_client.receive(local, sync.json())

    session.refresh(registry)
    assert registry.last_seen > old_report
    listed = client.get(f"/api/v1/fleet/sidecars/{registry_id}/sources")
    assert listed.status_code == 200
    status = listed.json()["sources"][0]
    assert status["source_id"] == source_id
    assert status["report_at"] == old_report.isoformat()
    assert status["report_stale"] is True
    assert status["last_read_at"] is None
    assert status["collection_health"] == "healthy"
    assert status["last_collection_at"] == success.isoformat()
    assert source_controls.allowed(session, local["device_id"], source_id, 0)
    assert len(await TokenCache().get_pending_sources(provider)) == 1

    response = post(client, local, "/api/v1/fleet/ingest", "ingest", credential_payload)
    assert response.status_code == 200 and response.json()["credentials_rejected"] == 0
    refreshed = client.get(f"/api/v1/fleet/sidecars/{registry_id}/sources")
    assert refreshed.status_code == 200
    updated = refreshed.json()["sources"][0]
    assert datetime.fromisoformat(updated["report_at"]) > old_report
    assert updated["report_stale"] is False
    assert updated["collection_health"] == "healthy"
    assert updated["last_collection_at"] == success.isoformat()
