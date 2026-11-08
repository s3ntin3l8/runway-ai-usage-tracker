"""Integration tests for discovered accounts, OpenCode auth discovery, and Anthropic Console discovery."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, select

from tests.fixtures.test_client import TestClient
from tests.integration._multi_account_provider_config_helpers import (
    CONSOLE_ORG_ID,
    _admin_headers,
    _mock_console_access,
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)


def test_opencode_saved_strategy_ids_are_normalized(client: TestClient):
    r = client.put(
        "/api/v1/system/provider-config/opencode/default",
        json={
            "collection_strategies": [
                {"id": "sidecar", "enabled": True},
                {"id": "api", "enabled": False},
                {"id": "web", "enabled": True},
            ]
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    opencode = next(
        p
        for p in client.get("/api/v1/system/provider-configs").json()["providers"]
        if p["provider_id"] == "opencode"
    )
    assert {s["id"] for s in opencode["supported_strategies"]} == {"api", "web"}
    assert {s["id"] for s in opencode["collection_strategies"]} == {"api", "web"}


def test_sidecar_source_label_uses_rule_and_token_metadata(client: TestClient):
    import time

    from app.services.token_cache import token_cache

    token_cache.seed_sync(
        "opencode",
        "oc-account",
        {"api_key": "secret-opencode-key"},  # pragma: allowlist secret
        {"source": "sidecar-host"},
        time.time(),
    )
    opencode = next(
        p
        for p in client.get("/api/v1/system/provider-configs").json()["providers"]
        if p["provider_id"] == "opencode"
    )
    account = next(a for a in opencode["accounts"] if a["account_id"] == "oc-account")
    assert "auth.json · API key" in account["credential_source_labels"]
    assert "secret-opencode-key" not in str(account)


@pytest.mark.parametrize(
    ("provider_id", "tokens", "source_description"),
    [
        (
            "opencode",
            {"api_key": "test-opencode-value"},  # pragma: allowlist secret
            "auth.json · API key",
        ),
        (
            "minimax",
            {"api_key": "test-minimax-value"},  # pragma: allowlist secret
            "auth.json · API key",
        ),
        (
            "xai",
            {"xai_access": "test-xai-access", "xai_refresh": "test-xai-refresh"},
            "auth.json · OAuth token",
        ),
    ],
)
def test_opencode_auth_json_discovery_is_shown_for_each_provider(
    client: TestClient,
    session: Session,
    provider_id: str,
    tokens: dict[str, str],
    source_description: str,
):
    import time

    from app.models.db import CredentialSource
    from app.services.token_cache import token_cache

    source_id = f"sidecar:auth-json-{provider_id}"
    now = time.time()
    source_metadata = {
        "source": "sidecar",
        "source_id": source_id,
        "source_type": "file",
        "source_label": "auth.json",
        "credential_origin": "path:/home/alice/.local/share/opencode/auth.json",
        "sidecar_id": "dev-01",
    }
    token_cache.seed_sync(
        provider_id,
        "alice@example.com",
        tokens,
        {"source": "sidecar"},
        now,
    )
    token_cache._source_cache.setdefault(provider_id, {}).setdefault("alice@example.com", {})[
        source_id
    ] = (tokens, source_metadata, now)
    session.add(
        CredentialSource(
            provider_id=provider_id,
            account_id="alice@example.com",
            source_id=source_id,
            source_type="file",
            source_label="auth.json",
            credential_origin=source_metadata["credential_origin"],
            sidecar_id="dev-01",
        )
    )
    session.commit()

    providers = client.get("/api/v1/system/provider-configs").json()["providers"]
    provider = next(p for p in providers if p["provider_id"] == provider_id)
    account = next(a for a in provider["accounts"] if a["account_id"] == "alice@example.com")

    assert source_description in account["credential_source_labels"]
    source = account["credential_sources"][0]
    assert source["source_label"] == "auth.json"
    assert source["sidecar_id"] == "dev-01"
    assert source["available"] is True
    assert source["health"] == "untried"  # registered by the manifest, never collected
    assert not any(secret in str(account) for secret in tokens.values())


def test_discovery_credential_kind_matches_known_oauth_token_types():
    from app.api.endpoints.system import _discovery_credential_kind

    assert _discovery_credential_kind("cookie", {"api_key"}) == "Cookie"
    assert _discovery_credential_kind("file", {"oauth_token"}) == "OAuth token"
    assert _discovery_credential_kind("file", {"id_token"}) == "OAuth token"
    assert _discovery_credential_kind("file", {"access_token"}) == "OAuth token"
    assert _discovery_credential_kind("file", {"xai_access"}) == "OAuth token"
    assert _discovery_credential_kind("file", {"oauth_flow"}) == "API key"


def test_stale_latest_usage_row_is_excluded_from_discovered_accounts(
    client: TestClient, session: Session
):
    """A stale latest_usage row must not surface as an auto-discovered account.

    When a transient or misidentified account collector fails or stops, the
    resulting stale card in latest_usage must be ignored by GET /provider-configs
    so phantom cards do not appear in the dashboard.
    """
    from datetime import UTC, datetime

    from app.models.db import LatestUsage

    stale_row = LatestUsage(
        provider_id="github",
        account_id="stale_ghost@example.com",
        window_type="weekly",
        variant="default",
        model_id="copilot",
        card_json='{"stale": true, "collection_failing": true}',
        updated_at=datetime.now(UTC),
    )
    active_row = LatestUsage(
        provider_id="github",
        account_id="active_user",
        window_type="weekly",
        variant="default",
        model_id="copilot",
        card_json='{"stale": false}',
        updated_at=datetime.now(UTC),
    )
    session.add(stale_row)
    session.add(active_row)
    session.commit()

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    github = next(p for p in listing if p["provider_id"] == "github")
    discovered_ids = [a["account_id"] for a in github["accounts"]]
    assert "active_user" in discovered_ids
    assert "stale_ghost@example.com" not in discovered_ids


def test_console_discovery_preview_and_save_keep_organization_identity(
    client, session, monkeypatch
):
    from app.models.db import ProviderConfig
    from app.services.account_identity import resolve_account_id

    _mock_console_access(monkeypatch)
    response = client.post(
        "/api/v1/system/provider-config/console-organizations",
        json={"session_cookie": "sample-session"},
    )
    assert response.status_code == 200
    assert response.json()["organizations"][0]["id"] == CONSOLE_ORG_ID
    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic_console",
            "session_cookie": "sample-session",
            "organization_id": CONSOLE_ORG_ID,
        },
    )
    assert preview.status_code == 200
    assert preview.json()["label_source"] == "organization"
    assert preview.json()["suggested_account_id"] == CONSOLE_ORG_ID
    path = f"/api/v1/system/provider-config/anthropic_console/{CONSOLE_ORG_ID}"
    saved = client.put(
        path, json={"session_cookie": "sample-session", "account_label": "alias@example.com"}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["account_id"] == CONSOLE_ORG_ID
    assert (
        resolve_account_id("anthropic_console", CONSOLE_ORG_ID, "alias@example.com")
        == CONSOLE_ORG_ID
    )
    row = session.exec(
        select(ProviderConfig).where(ProviderConfig.provider_id == "anthropic_console")
    ).one()
    assert row.billing_type == "pay_as_you_go"
    # The balance proved it; nobody chose it, and the source column says so —
    # the repair that prices the accounts predating detection must not
    # mistake a proof for a choice.
    assert row.billing_type_source == "detected"
    assert row.session_cookie == "sample-session"
    assert (
        client.post(
            "/api/v1/system/provider-config/preview-account",
            json={
                "provider_id": "anthropic_console",
                "session_cookie": "replacement-session",
                "organization_id": CONSOLE_ORG_ID,
            },
        ).status_code
        == 409
    )
    assert client.put(path, json={"session_cookie": "replacement-session"}).status_code == 200
    assert client.put(path, json={"clear_session_cookie": True}).status_code == 200
    session.refresh(row)
    assert row.session_cookie is None
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    console = next(p for p in listing if p["provider_id"] == "anthropic_console")
    assert console["supports_session_cookie"] and not console["supports_api_key"]
    assert "replacement-session" not in str(console)


def test_console_creation_requires_accessible_org_and_billing(client, monkeypatch):
    from app.api.endpoints import system_provider_config as system
    from app.services.collectors.anthropic_console import ConsoleAPIError

    _mock_console_access(monkeypatch)
    path = f"/api/v1/system/provider-config/anthropic_console/{CONSOLE_ORG_ID}"
    assert client.put(path, json={}).status_code == 422
    assert (
        client.put(
            "/api/v1/system/provider-config/anthropic_console/default",
            json={"session_cookie": "sample-session"},
        ).status_code
        == 422
    )
    monkeypatch.setattr(
        system,
        "console_get",
        AsyncMock(side_effect=ConsoleAPIError("Billing access denied", "auth_failed", 403)),
    )
    assert client.put(path, json={"session_cookie": "sample-session"}).status_code == 403
    monkeypatch.setattr(system, "console_organizations", AsyncMock(return_value=[]))
    assert client.put(path, json={"session_cookie": "sample-session"}).status_code == 403


def test_console_unchanged_cookie_allows_settings_edits_during_outage(client, session, monkeypatch):
    from fastapi import HTTPException

    from app.api.endpoints import system_provider_config as system
    from app.models.db import ProviderConfig

    _mock_console_access(monkeypatch)
    path = f"/api/v1/system/provider-config/anthropic_console/{CONSOLE_ORG_ID}"
    assert client.put(path, json={"session_cookie": "sample-session"}).status_code == 200
    validate = AsyncMock(side_effect=HTTPException(status_code=502, detail="Console unavailable"))
    monkeypatch.setattr(system, "_validate_console_connection", validate)

    response = client.put(
        path,
        json={"session_cookie": "sample-session", "enabled": False, "poll_interval_seconds": 1200},
    )
    assert response.status_code == 200, response.text
    validate.assert_not_awaited()
    row = session.exec(
        select(ProviderConfig).where(ProviderConfig.provider_id == "anthropic_console")
    ).one()
    session.refresh(row)
    assert row.enabled is False
    assert row.poll_interval_seconds == 1200
    assert row.session_cookie == "sample-session"

    assert client.put(path, json={"session_cookie": "replacement-session"}).status_code == 502
    validate.assert_awaited_once_with("replacement-session", CONSOLE_ORG_ID)
    session.refresh(row)
    assert row.session_cookie == "sample-session"

    validate.reset_mock()
    assert (
        client.put(
            path, json={"session_cookie": "sample-session", "clear_session_cookie": True}
        ).status_code
        == 200
    )
    validate.assert_not_awaited()
    session.refresh(row)
    assert row.session_cookie is None


def test_console_preview_normalizes_org_before_duplicate_check(client, monkeypatch):
    _mock_console_access(monkeypatch)
    noncanonical = CONSOLE_ORG_ID.replace("-", "")
    payload = {
        "provider_id": "anthropic_console",
        "session_cookie": "sample-session",
        "organization_id": noncanonical,
    }
    preview = client.post("/api/v1/system/provider-config/preview-account", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["suggested_account_id"] == CONSOLE_ORG_ID
    assert (
        client.put(
            f"/api/v1/system/provider-config/anthropic_console/{CONSOLE_ORG_ID}",
            json={"session_cookie": "sample-session"},
        ).status_code
        == 200
    )
    duplicate = client.post("/api/v1/system/provider-config/preview-account", json=payload)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"]["already_exists"] is True
    assert duplicate.json()["detail"]["suggested_account_id"] == CONSOLE_ORG_ID
    payload["organization_id"] = "invalid"
    assert (
        client.post("/api/v1/system/provider-config/preview-account", json=payload).status_code
        == 422
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["Chrome", "Safari", "Claude Desktop"])
async def test_cache_only_native_cookie_listing_identifies_client(client, owner):
    from app.services.token_cache import token_cache

    await token_cache.store(
        "anthropic",
        {"cookie_sessionKey": "synthetic-cookie"},
        account_id="synthetic@example.com",
        source_id="sidecar:cache-only",
        source_metadata={
            "source_type": "cookie",
            "sidecar_id": "test-mac",
            "credential_origin": f"cookie:anthropic/{owner}/private-profile/Cookies#private-fingerprint",
        },
    )
    response = client.get("/api/v1/system/provider-configs")
    assert response.status_code == 200
    provider = next(p for p in response.json()["providers"] if p["provider_id"] == "anthropic")
    account = next(a for a in provider["accounts"] if a["account_id"] == "synthetic@example.com")
    assert account["credential_sources"][0]["source_label"] == f"{owner} cookie"
    assert "synthetic-cookie" not in response.text
    assert "private-profile" not in response.text
    assert "private-fingerprint" not in response.text


def test_every_account_carries_the_provider_it_belongs_to(client: TestClient, session: Session):
    """Every account says which provider it belongs to.

    The Claude family groups several providers under one card, so an account
    listed under the family is still addressed by its own provider — an edit
    must never land on the family's representative (#723).
    """
    from app.models.db import ProviderConfig

    session.add(
        ProviderConfig(
            provider_id="anthropic_console",
            account_id="11111111-1111-4111-8111-111111111111",
            enabled=True,
            account_label="API org",
        )
    )
    session.commit()

    r = client.get("/api/v1/system/provider-configs")
    assert r.status_code == 200
    providers = r.json()["providers"]

    console = next(p for p in providers if p["provider_id"] == "anthropic_console")
    org = next(
        a for a in console["accounts"] if a["account_id"] == "11111111-1111-4111-8111-111111111111"
    )
    assert org["provider_id"] == "anthropic_console"

    # No account of any provider may claim another provider.
    for provider in providers:
        for account in provider["accounts"]:
            assert account["provider_id"] == provider["provider_id"]
