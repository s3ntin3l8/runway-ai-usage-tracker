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
    monkeypatch.setattr(credential_inventory, "_scan_server_credentials", lambda: ({}, set()))
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


@pytest.mark.asyncio
async def test_delete_refuses_a_live_config_bundle_even_without_a_durable_row(client, cache):
    await cache.store(
        "gemini",
        {"api_key": "pasted"},  # pragma: allowlist secret
        account_id=ALICE,
        source_id="config:gemini:alice",
    )
    resp = client.delete(
        f"/api/v1/system/credentials/gemini/{ALICE}/config:gemini:alice", headers=_headers()
    )
    assert resp.status_code == 409
    assert len(await cache.get_source_candidates("gemini", ALICE)) == 1


def _server_origin(exp=None, **kw):
    return {
        "source_type": "file",
        "label": "oauth_creds.json",
        "keys": ["oauth_token"],
        "managed": False,
        "shadowed": False,
        "exp": exp,
        "rollable": False,
        **kw,
    }


def _inventory_server_row(client):
    body = client.get("/api/v1/system/credentials", headers=_headers()).json()
    (row,) = [
        s
        for p in body["providers"]
        for a in p["accounts"]
        for s in a["sources"]
        if s["origin_kind"] == "server"
    ]
    return row


def test_a_registered_server_row_keeps_a_durable_rejection_when_its_credential_is_replaced(
    client, session, monkeypatch
):
    """The row was rejected and its stored expiry has passed; the host has since swapped in
    a fresh credential. The rejection (recorded at the last collection) must still show."""
    import time
    from datetime import UTC, datetime, timedelta

    _add(
        session,
        provider_id="gemini",
        account_id="default",
        source_id="server:gemini:file:oauth_creds.json",
        source_type="file",
        source_label="oauth_creds.json",
        credential_origin=None,
        sidecar_id=None,
        health="auth_failed",
        credential_expires_at=datetime.now(UTC) - timedelta(days=1),
        token_types_json='["oauth_token"]',
    )
    origin = _server_origin(exp=time.time() + 3600)
    monkeypatch.setattr(
        credential_inventory,
        "_scan_server_credentials",
        lambda: ({"gemini": [origin]}, {"gemini"}),
    )

    row = _inventory_server_row(client)

    assert row["status"] == "invalid"
    assert row["expires_in_seconds"] > 0, "the expiry shown is the host's current credential"


def test_a_registered_server_row_drops_a_stale_expiry_the_scan_no_longer_sees(
    client, session, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    _add(
        session,
        provider_id="zai",
        account_id="default",
        source_id="server:zai:env:ZAI_API_KEY",
        source_type="env",
        source_label="ZAI_API_KEY",
        credential_origin=None,
        sidecar_id=None,
        credential_expires_at=datetime.now(UTC) - timedelta(days=1),
        token_types_json='["api_key"]',
    )
    origin = _server_origin(source_type="env", label="ZAI_API_KEY", keys=["api_key"], exp=None)
    monkeypatch.setattr(
        credential_inventory, "_scan_server_credentials", lambda: ({"zai": [origin]}, {"zai"})
    )

    row = _inventory_server_row(client)

    assert row["status"] == "valid"
    assert row["expires_at"] is None


@pytest.mark.asyncio
async def test_forgetting_the_last_source_clears_the_accounts_rejection_flag(
    client, session, cache
):
    from app.services import auth_failures

    auth_failures.reset()
    _add(session)
    _add(session, source_id="sidecar:b", sidecar_id="host-b")
    await cache.store("gemini", {"oauth_token": "t"}, account_id=ALICE, source_id="sidecar:a")
    auth_failures.mark("gemini", ALICE)

    client.delete(f"/api/v1/system/credentials/gemini/{ALICE}/sidecar:a", headers=_headers())
    assert auth_failures.flagged_accounts("gemini") == {ALICE}, "another source still stands"

    client.delete(f"/api/v1/system/credentials/gemini/{ALICE}/sidecar:b", headers=_headers())
    assert auth_failures.flagged_accounts("gemini") == set()


def test_inventory_warns_about_unmapped_credentials_that_stop_collection(client, session):
    from app.models.db import PendingCredentialTag
    from app.services.credential_tags import CredentialTagRepo

    for origin in ("env:A_KEY", "env:B_KEY"):
        session.add(
            PendingCredentialTag(
                sidecar_id="host-a",
                provider_id="deepseek",
                credential_origin=origin,
                reason="token_withheld",
            )
        )
    session.commit()

    body = client.get("/api/v1/system/credentials", headers=_headers()).json()
    assert body["unmapped_count"] == 2
    assert {b["credential_origin"] for b in body["blocked_collection"]} == {
        "env:A_KEY",
        "env:B_KEY",
    }
    assert all(
        b["sidecar_id"] == "host-a" and b["provider_id"] == "deepseek"
        for b in body["blocked_collection"]
    )

    # Once an operator tags one, it is no longer waiting: the count and the warning drop it,
    # exactly as the Untagged list does.
    CredentialTagRepo.set_tag(
        session,
        provider_id="deepseek",
        credential_origin="env:A_KEY",
        account_id="alice@example.com",
        sidecar_id="host-a",
    )
    session.commit()
    body = client.get("/api/v1/system/credentials", headers=_headers()).json()
    assert body["unmapped_count"] == 1
    assert [b["credential_origin"] for b in body["blocked_collection"]] == ["env:B_KEY"]


@pytest.mark.asyncio
async def test_bulk_remove_forgets_machine_sources_and_skips_managed_ones(client, session, cache):
    for sid, host in (("sidecar:a", "host-a"), ("sidecar:b", "host-b"), ("sidecar:c", "host-c")):
        _add(session, source_id=sid, sidecar_id=host)
    _add(session, source_id="config:gemini:alice", sidecar_id=None, source_type="config")
    await cache.store("gemini", {"oauth_token": "tok"}, account_id=ALICE, source_id="sidecar:a")

    resp = client.post(
        f"/api/v1/system/credentials/gemini/{ALICE}/remove",
        json={"source_ids": ["sidecar:a", "sidecar:b", "config:gemini:alice", "sidecar:nope"]},
        headers=_headers(),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["removed"] == ["sidecar:a", "sidecar:b"]
    assert body["skipped"] == [
        {"source_id": "config:gemini:alice", "reason": "managed_elsewhere"},
        {"source_id": "sidecar:nope", "reason": "not_found"},
    ]
    assert {r.source_id for r in session.exec(select(CredentialSource)).all()} == {
        "sidecar:c",
        "config:gemini:alice",
    }
    assert await cache.get_source_candidates("gemini", ALICE) == []


@pytest.mark.asyncio
async def test_bulk_remove_keeps_the_active_login_that_a_dead_copy_shares(client, session, cache):
    _add(session, source_id="sidecar:live", sidecar_id="host-a")
    _add(session, source_id="sidecar:dead", sidecar_id="host-b")
    shared = {
        "oauth_token": "same-access",
        "refresh_token": "same-refresh",
    }  # pragma: allowlist secret
    await cache.store("gemini", dict(shared), account_id=ALICE, source_id="sidecar:live")
    await cache.store("gemini", dict(shared), account_id=ALICE, source_id="sidecar:dead")

    resp = client.post(
        f"/api/v1/system/credentials/gemini/{ALICE}/remove",
        json={"source_ids": ["sidecar:dead"]},
        headers=_headers(),
    )

    assert resp.json()["removed"] == ["sidecar:dead"]
    assert (await cache.get("gemini", ALICE))["oauth_token"] == "same-access"
    assert [c["source_id"] for c in await cache.get_source_candidates("gemini", ALICE)] == [
        "sidecar:live"
    ]


def test_bulk_remove_rejects_an_empty_or_oversized_batch(client):
    url = f"/api/v1/system/credentials/gemini/{ALICE}/remove"
    assert client.post(url, json={"source_ids": []}, headers=_headers()).status_code == 422
    too_many = {"source_ids": [f"sidecar:{i}" for i in range(51)]}
    assert client.post(url, json=too_many, headers=_headers()).status_code == 422
