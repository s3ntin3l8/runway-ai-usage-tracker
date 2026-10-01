"""`/system/credentials` — the inventory plus per-source refresh and removal."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.main import app
from app.models.db import CredentialSource
from app.services import credential_inventory
from app.services.token_cache import TokenCache

ALICE = "alice@example.com"


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture(name="cache")
def cache_fixture(monkeypatch) -> TokenCache:
    fresh = TokenCache()
    monkeypatch.setattr("app.api.endpoints.system.token_cache", fresh)
    monkeypatch.setattr(credential_inventory, "token_cache", fresh)
    return fresh


@pytest.fixture(name="client")
def client_fixture(session: Session, cache, monkeypatch):
    monkeypatch.setattr(credential_inventory, "engine", session.get_bind())
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _headers() -> dict[str, str]:
    from app.core.config import settings

    return {"X-Admin-Key": settings.ADMIN_API_KEY} if settings.ADMIN_API_KEY else {}


def _add(session: Session, **kw) -> None:
    session.add(
        CredentialSource(
            **{
                "provider_id": "gemini",
                "account_id": ALICE,
                "source_id": "sidecar:a",
                "source_type": "file",
                "source_label": "oauth_creds.json",
                "credential_origin": "path:/x/oauth_creds.json",
                "sidecar_id": "host-a",
                **kw,
            }
        )
    )
    session.commit()


def test_get_inventory_returns_typed_payload(client, session):
    _add(session)
    resp = client.get("/api/v1/system/credentials", headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (provider,) = body["providers"]
    assert provider["provider_id"] == "gemini"
    (account,) = provider["accounts"]
    (source,) = account["sources"]
    assert source["source_id"] == "sidecar:a" and source["origin_kind"] == "machine"
    assert set(body) >= {"machines", "unmapped_count", "rule_count", "pending_usage_events"}


@pytest.mark.asyncio
async def test_refresh_writes_back_into_the_source_bundle(client, session, cache):
    _add(session)
    await cache.store(
        "gemini",
        {"oauth_token": "old", "refresh_token": "rt1"},
        account_id=ALICE,
        source_id="sidecar:a",
    )
    refreshed = AsyncMock(return_value={"oauth_token": "new", "refresh_token": "rt2"})
    with patch("app.services.token_refresher.refresh_oauth_token", new=refreshed):
        resp = client.post(
            f"/api/v1/system/credentials/gemini/{ALICE}/sidecar:a/refresh", headers=_headers()
        )

    assert resp.status_code == 200, resp.text
    (bundle,) = await cache.get_source_candidates("gemini", ALICE)
    assert bundle["tokens"]["oauth_token"] == "new"
    assert bundle["tokens"]["refresh_token"] == "rt2"


@pytest.mark.asyncio
async def test_refresh_unknown_source_404_and_no_refresh_token_400(client, session, cache):
    unknown = client.post(
        f"/api/v1/system/credentials/gemini/{ALICE}/sidecar:nope/refresh", headers=_headers()
    )
    assert unknown.status_code == 404

    await cache.store("gemini", {"oauth_token": "only"}, account_id=ALICE, source_id="sidecar:a")
    no_rt = client.post(
        f"/api/v1/system/credentials/gemini/{ALICE}/sidecar:a/refresh", headers=_headers()
    )
    assert no_rt.status_code == 400


@pytest.mark.asyncio
async def test_delete_removes_bundle_and_durable_row_for_machine_source(client, session, cache):
    _add(session)
    _add(session, source_id="sidecar:b", sidecar_id="host-b")
    await cache.store("gemini", {"oauth_token": "tok"}, account_id=ALICE, source_id="sidecar:a")

    resp = client.delete(f"/api/v1/system/credentials/gemini/{ALICE}/sidecar:a", headers=_headers())

    assert resp.status_code == 200, resp.text
    assert await cache.get_source_candidates("gemini", ALICE) == []
    remaining = {r.source_id for r in session.exec(select(CredentialSource)).all()}
    assert remaining == {"sidecar:b"}  # the other machine's credential is untouched


def test_delete_refuses_config_and_server_sources(client, session):
    _add(session, source_id="config:gemini:alice", sidecar_id=None, source_type="config")
    _add(session, source_id="server:gemini:env:KEY", sidecar_id=None, source_type="env")
    for source_id in ("config:gemini:alice", "server:gemini:env:KEY"):
        resp = client.delete(
            f"/api/v1/system/credentials/gemini/{ALICE}/{source_id}", headers=_headers()
        )
        assert resp.status_code == 409
        assert "Settings" in resp.json()["detail"]
    assert len(session.exec(select(CredentialSource)).all()) == 2


def test_delete_unknown_source_is_404(client):
    resp = client.delete(
        f"/api/v1/system/credentials/gemini/{ALICE}/sidecar:nope", headers=_headers()
    )
    assert resp.status_code == 404
