"""Integration tests for credential source preferences, host scoping, and source audit logging."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, select

from app.models.db import AuditLog, CredentialSource
from tests.fixtures.test_client import TestClient
from tests.integration._multi_account_provider_config_helpers import (
    _admin_headers,
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)


@pytest.mark.asyncio
async def test_credential_source_preferences_and_safe_listing(client: TestClient, session: Session):
    from app.services.token_cache import token_cache

    session.add_all(
        [
            CredentialSource(
                provider_id="openrouter",
                account_id="alice@example.com",
                source_id="sidecar:first",
                source_type="file",
                source_label="OpenCode auth.json",
                sidecar_id="host-a",
                credential_origin="path:/auth.json",
                priority=0,
            ),
            CredentialSource(
                provider_id="openrouter",
                account_id="alice@example.com",
                source_id="sidecar:second",
                source_type="env",
                source_label="OPENROUTER_API_KEY",
                sidecar_id="host-b",
                priority=1,
            ),
        ]
    )
    session.commit()
    await token_cache.store(
        "openrouter",
        {"api_key": "private-one"},  # pragma: allowlist secret — assert response redaction
        account_id="alice@example.com",
        source_id="sidecar:first",
        source_metadata={
            "source_type": "file",
            "sidecar_id": "host-a",
            "credential_origin": "path:/auth.json",
            "source_label": "OpenCode auth.json",
        },
    )
    await token_cache.store(
        "openrouter",
        {"api_key": "private-two"},  # pragma: allowlist secret — assert response redaction
        account_id="alice@example.com",
        source_id="sidecar:second",
        source_metadata={"source_type": "env", "source_label": "OPENROUTER_API_KEY"},
    )

    response = client.patch(
        "/api/v1/system/provider-config/openrouter/alice@example.com/credential-sources",
        json={
            "sources": [
                {"source_id": "sidecar:second", "enabled": True, "priority": 0},
                {"source_id": "sidecar:first", "enabled": False, "priority": 1},
            ]
        },
    )
    assert response.status_code == 200, response.text
    rows = session.exec(
        select(CredentialSource).where(CredentialSource.provider_id == "openrouter")
    ).all()
    by_id = {row.source_id: row for row in rows}
    assert by_id["sidecar:second"].priority == 0
    assert by_id["sidecar:first"].enabled is False

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    account = next(p for p in listing if p["provider_id"] == "openrouter")["accounts"][0]
    assert [source["source_id"] for source in account["credential_sources"]] == [
        "sidecar:second",
        "sidecar:first",
    ]
    assert "private-one" not in str(account)
    assert "private-two" not in str(account)


def test_credential_source_listing_names_a_cookie_row_a_cookie(
    client: TestClient, session: Session
):
    """A row an older ingest stored as ``sidecar`` still reads as the cookie it is:
    the listing describes the row from its origin, so the account editor can tell
    a browser session from a credential file."""
    created = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert created.status_code == 200, created.text
    session.add(
        CredentialSource(
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="sidecar:cookie",
            source_type="sidecar",  # what ingest stored before cookies had their own type
            source_label="Browser cookie",
            credential_origin="cookie:openrouter/Chrome/Profile 1/Cookies",
            sidecar_id="host-a",
        )
    )
    session.commit()

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    account = next(p for p in listing if p["provider_id"] == "openrouter")["accounts"][0]
    source = next(s for s in account["credential_sources"] if s["source_id"] == "sidecar:cookie")
    assert source["source_type"] == "cookie"
    assert source["source_label"] == "Browser cookie"
    assert source["source_detail"] == "Chrome · Profile 1"


def test_credential_source_preferences_are_host_scoped_unless_all_machines(
    client: TestClient, session: Session, monkeypatch
):
    from app.services.collector_manager import manager

    monkeypatch.setattr(manager, "credential_source_preferences", {})
    origin = "path:~/.config/provider/auth.json"
    session.add_all(
        [
            CredentialSource(
                provider_id="openrouter",
                account_id="alice@example.com",
                source_id="host-a-source",
                source_type="file",
                source_label="auth.json",
                credential_origin=origin,
                sidecar_id="host-a",
            ),
            CredentialSource(
                provider_id="openrouter",
                account_id="alice@example.com",
                source_id="host-b-source",
                source_type="file",
                source_label="auth.json",
                credential_origin=origin,
                sidecar_id="host-b",
            ),
            CredentialSource(
                provider_id="openrouter",
                account_id="bob@example.com",
                source_id="host-c-source",
                source_type="file",
                source_label="auth.json",
                credential_origin=origin,
                sidecar_id="host-c",
            ),
        ]
    )
    session.commit()
    endpoint = "/api/v1/system/provider-config/openrouter/alice@example.com/credential-sources"
    payload = {
        "sources": [
            {"source_id": "host-a-source", "enabled": False, "priority": 3},
            {"source_id": "host-b-source", "enabled": True, "priority": 1},
        ]
    }
    response = client.patch(endpoint, json=payload)
    assert response.status_code == 200, response.text
    assert manager.credential_source_preferences[("openrouter", "alice@example.com")] == {
        "host-a-source": (False, 3),
        "host-b-source": (True, 1),
    }
    session.expire_all()
    host_b = session.exec(
        select(CredentialSource).where(CredentialSource.source_id == "host-b-source")
    ).one()
    assert host_b.enabled is True

    all_machines_payload = {
        "sources": [
            {"source_id": "host-a-source", "enabled": False, "priority": 3},
            {"source_id": "host-b-source", "enabled": True, "priority": 1},
        ],
        "all_machines": True,
    }
    response = client.patch(endpoint, json=all_machines_payload)
    assert response.status_code == 200, response.text
    assert manager.credential_source_preferences[("openrouter", "alice@example.com")] == {
        "host-a-source": (False, 3),
        "host-b-source": (True, 1),
    }
    session.expire_all()
    siblings = session.exec(
        select(CredentialSource).where(CredentialSource.credential_origin == origin)
    ).all()
    assert {(row.sidecar_id, row.account_id, row.enabled, row.priority) for row in siblings} == {
        ("host-a", "alice@example.com", False, 3),
        ("host-b", "alice@example.com", True, 1),
        ("host-c", "bob@example.com", True, 0),
    }


def test_credential_source_preferences_reject_unknown_and_duplicate_ids(
    client: TestClient, session: Session
):
    session.add(
        CredentialSource(
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="known-source",
            source_type="file",
            source_label="auth.json",
            sidecar_id="test-sidecar",
            credential_origin="path:/auth.json",
        )
    )
    session.add(
        CredentialSource(
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="omitted-source",
            source_type="file",
            source_label="other.json",
            sidecar_id="test-sidecar",
            credential_origin="path:/other.json",
            enabled=False,
            priority=1,
        )
    )
    session.commit()
    endpoint = "/api/v1/system/provider-config/openrouter/alice@example.com/credential-sources"
    preference = {"source_id": "known-source", "enabled": False, "priority": 0}
    duplicate = client.patch(endpoint, json={"sources": [preference, preference]})
    partial = client.patch(endpoint, json={"sources": [preference]})
    unknown = client.patch(
        endpoint,
        json={"sources": [{"source_id": "unknown-source", "enabled": True, "priority": 0}]},
    )
    unknown_provider = client.patch(
        "/api/v1/system/provider-config/not-a-provider/alice@example.com/credential-sources",
        json={"sources": []},
    )

    assert duplicate.status_code == 422
    assert partial.status_code == 422
    assert "include every known source" in partial.json()["detail"]
    assert unknown.status_code == 404
    assert unknown_provider.status_code == 404
    session.expire_all()
    known_source = session.exec(
        select(CredentialSource).where(CredentialSource.source_id == "known-source")
    ).one()
    omitted_source = session.exec(
        select(CredentialSource).where(CredentialSource.source_id == "omitted-source")
    ).one()
    assert known_source.enabled is True
    assert (omitted_source.enabled, omitted_source.priority) == (False, 1)


def test_credential_source_audit_is_committed_when_collector_reset_fails(
    client: TestClient, session: Session, monkeypatch
):
    session.add(
        CredentialSource(
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="source-a",
            source_type="file",
            source_label="Laptop",
            sidecar_id="test-sidecar",
            credential_origin="path:/auth.json",
        )
    )
    session.commit()
    monkeypatch.setattr(
        "app.api.endpoints.system_provider_config.manager.reset_collector",
        AsyncMock(side_effect=RuntimeError("reset failed")),
    )

    response = client.patch(
        "/api/v1/system/provider-config/openrouter/alice@example.com/credential-sources",
        json={"sources": [{"source_id": "source-a", "enabled": False, "priority": 0}]},
        headers=_admin_headers(),
    )

    assert response.status_code == 200, response.text
    session.expire_all()
    source = session.exec(
        select(CredentialSource).where(CredentialSource.source_id == "source-a")
    ).one()
    assert source.enabled is False
    audit = session.exec(
        select(AuditLog).where(AuditLog.action == "credential.sources_update")
    ).one()
    assert json.loads(audit.payload_json or "{}")["sources"][0]["source_id"] == "source-a"
