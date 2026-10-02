"""Multi-account hardening tests for the provider config endpoints.

Provider updates require an explicit account id in the URL. These tests pin
the canonical per-account route and the GET response's ``accounts`` field.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.main import app
from app.models.db import AuditLog, CredentialSource, LatestUsage


@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    os.close(fd)
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    # list_provider_configs unions token_cache into `accounts` — clear the
    # in-memory cache so empty-state assertions aren't polluted by prior tests.
    from app.services.token_cache import token_cache

    token_cache._cache.clear()
    token_cache._source_cache.clear()
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()
    token_cache._cache.clear()
    token_cache._source_cache.clear()


def _admin_headers() -> dict[str, str]:
    """Set the X-Admin-Key the PUT endpoint requires."""
    from app.core.config import settings

    if settings.ADMIN_API_KEY:
        return {"X-Admin-Key": settings.ADMIN_API_KEY}
    return {}


def test_get_provider_configs_empty_accounts_field(client: TestClient):
    """No config rows: each provider's `accounts` list is empty + account_count=0."""
    r = client.get("/api/v1/system/provider-configs")
    assert r.status_code == 200
    providers = r.json()["providers"]
    assert providers  # registry has at least one entry
    xai = next(p for p in providers if p["provider_id"] == "xai")
    assert xai["supports_api_key"] is True
    for p in providers:
        assert p["accounts"] == []
        assert p["account_count"] == 0


def test_get_provider_configs_merges_cache_seeded_accounts(client: TestClient):
    """Sidecar-seeded token_cache accounts (no provider_configs row) appear
    as source="discovered" with account_count>=1 — the antigravity bug (#294)."""
    import time

    from app.services.token_cache import token_cache

    # seed_sync writes the same (tokens, meta, last_seen) layout as store()
    # without touching the asyncio lock (await store() from a throwaway loop
    # would bind token_cache._lock to that loop).
    token_cache.seed_sync(
        "antigravity",
        "user@example.com",
        {"refresh_token": "ag-refresh-token"},  # pragma: allowlist secret
        {"account_label": "User", "source": "sidecar"},
        time.time(),
    )
    try:
        r = client.get("/api/v1/system/provider-configs")
        assert r.status_code == 200
        antigravity = next(p for p in r.json()["providers"] if p["provider_id"] == "antigravity")
        assert antigravity["account_count"] >= 1
        discovered = [a for a in antigravity["accounts"] if a["source"] == "discovered"]
        assert discovered, antigravity["accounts"]
        assert any(a["account_id"] == "user@example.com" for a in discovered)
        assert antigravity["account_count"] > 0
    finally:
        token_cache._cache.clear()


def test_provider_update_requires_explicit_account_id(client: TestClient):
    r = client.put(
        "/api/v1/system/provider-config/openrouter",
        json={"account_label": "ambiguous"},
        headers=_admin_headers(),
    )
    assert r.status_code == 405


def test_explicit_put_creates_account_id(client: TestClient):
    """The per-account endpoint creates the row at the supplied account_id."""
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["account_id"] == "alice@example.com"

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    assert openrouter["accounts"][0]["account_id"] == "alice@example.com"


def test_explicit_put_creates_second_account_for_same_provider(client: TestClient):
    """A second per-account PUT adds a second row, doesn't overwrite."""
    a = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    b = client.put(
        "/api/v1/system/provider-config/openrouter/bob@example.com",
        json={"account_label": "Bob"},
        headers=_admin_headers(),
    )
    assert a.status_code == 200
    assert b.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 2
    account_ids = {row["account_id"] for row in openrouter["accounts"]}
    assert account_ids == {"alice@example.com", "bob@example.com"}


def test_explicit_put_with_hash_account_id_and_email_label_adopts_email(client: TestClient):
    """When a new config is saved with a credential hash ID and an email label,
    the endpoint adopts the email as the authoritative account_id."""
    hash_id = "1692b86a1d20fa0877d379ee299ad1e90b06b2c0ef0c3021d2a75c5fb5bac2ef"
    resp = client.put(
        f"/api/v1/system/provider-config/openrouter/{hash_id}",
        json={"account_label": "User@Example.com"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["account_id"] == "user@example.com"
    assert resp.json()["original_account_id"] == hash_id

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert any(row["account_id"] == "user@example.com" for row in openrouter["accounts"])
    assert not any(row["account_id"] == hash_id for row in openrouter["accounts"])


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


def test_credential_source_preferences_are_host_scoped_unless_all_machines(
    client: TestClient, session: Session, monkeypatch
):
    from app.services.collector_manager import manager

    monkeypatch.setattr(manager, "_credential_source_preferences", {})
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
    assert manager._credential_source_preferences[("openrouter", "alice@example.com")] == {
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
    assert manager._credential_source_preferences[("openrouter", "alice@example.com")] == {
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
        "app.api.endpoints.system.manager.reset_collector",
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


def test_explicit_put_preserves_canonical_field_derivation(client: TestClient):
    """The top-level (legacy) fields come from the 'default' row when present,
    or the first row when no 'default' row exists."""
    # Two accounts, neither at 'default'.
    client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    client.put(
        "/api/v1/system/provider-config/openrouter/bob@example.com",
        json={"account_label": "Bob"},
        headers=_admin_headers(),
    )

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")

    # account_count = 2
    assert openrouter["account_count"] == 2
    # The legacy top-level account_label comes from the FIRST row (no 'default'
    # row to anchor on). The webapp is expected to migrate to consuming the
    # `accounts` field; this assertion only pins the canonical-row rule.
    canonical_account_ids = [r["account_id"] for r in openrouter["accounts"]]
    assert openrouter["account_label"] == {"alice@example.com": "Alice"}.get(
        canonical_account_ids[0]
    )


def test_explicit_put_unknown_provider_returns_404(client: TestClient):
    r = client.put(
        "/api/v1/system/provider-config/no-such-provider/alice@example.com",
        json={"account_label": "x"},
        headers=_admin_headers(),
    )
    assert r.status_code == 404
    assert "no-such-provider" in r.json()["detail"]


def test_explicit_put_with_default_account_id_creates_default_row(client: TestClient):
    """Saving via PUT /provider-config/{pid}/default produces the same row
    that single-account installations use for default credentials."""
    r = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"account_label": "via explicit"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    assert openrouter["accounts"][0]["account_id"] == "default"
    assert openrouter["accounts"][0]["account_label"] == "via explicit"


def _seed_latest_usage(session: Session, provider_id: str, account_id: str) -> None:
    """Insert a synthetic `latest_usage` row so the GET /provider-configs
    endpoint sees the (provider_id, account_id) pair as having live data."""
    from datetime import UTC, datetime

    from app.models.db import LatestUsage

    row = LatestUsage(
        provider_id=provider_id,
        account_id=account_id,
        window_type="weekly",
        variant="default",
        model_id="claude-opus-4-7",
        card_json="{}",
        updated_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()


def test_is_orphaned_false_for_default_row_when_no_live_data(client: TestClient):
    """Default row with no live data and no sibling: NOT orphaned.

    Just-configured providers and providers whose collection is failing
    both hit this state — flagging it as 'safe to remove' would risk
    deleting the user's only credential. Gating on a non-default sibling
    means the dialog only shows the hint when there's a real replacement.
    """
    r = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"account_label": "only row"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    default_row = openrouter["accounts"][0]
    assert default_row["account_id"] == "default"
    assert default_row["is_orphaned"] is False


def test_preview_account_returns_409_when_identity_already_exists(client: TestClient):
    """Existing account under the suggested id → 409 from the endpoint."""
    # Seed one row under alice@example.com.
    client.put(
        "/api/v1/system/provider-config/anthropic/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )

    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic",
            "api_key": "alice@example.com",  # email-shaped → suggests same id  # pragma: allowlist secret
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 409
    # The 409 body wraps the preview detail in `detail` (FastAPI's HTTPException
    # convention). The wizard reads the same fields from there.
    detail = r.json()["detail"]
    assert detail["already_exists"] is True
    assert detail["suggested_account_id"] == "alice@example.com"


def test_preview_account_returns_default_when_no_credential(client: TestClient):
    """No api_key + no session_cookie → default identity, no 4xx."""
    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "anthropic"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["suggested_account_id"] == "default"
    assert data["label_source"] == "default"


def test_preview_account_rejects_unknown_provider(client: TestClient):
    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "no-such-provider", "api_key": "x"},
        headers=_admin_headers(),
    )
    assert r.status_code == 404


def test_clear_api_key_wipes_encrypted_blob(client: TestClient):
    """Setting ``clear_api_key=True`` on the explicit PUT wipes the stored
    key. The next GET reports ``api_key_set=false``."""
    # Seed a row with an api_key.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"api_key": "sk-test-123", "account_label": "only row"},  # pragma: allowlist secret
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    default_row = openrouter["accounts"][0]
    assert default_row["account_id"] == "default"
    assert default_row["api_key_set"] is True

    # Clear the key.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"clear_api_key": True},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    default_row = openrouter["accounts"][0]
    assert default_row["api_key_set"] is False


def _cache_tokens(provider: str, account_id: str) -> dict:
    """Tokens cached for a provider/account (empty dict when absent)."""
    from app.services.token_cache import token_cache

    entry = token_cache._cache.get(provider, {}).get(account_id)
    return entry[0] if entry else {}


def test_kimi_api_key_explicit_put_mirrors_to_token_cache(client: TestClient):
    """Issue #343: a dashboard-pasted kimi_coding key must reach the token
    cache under the ``api_key`` slot — the collector resolves it from there
    (an account-keyed row never reaches an unscoped DB read), and the
    cache is what drives dynamic-collector discovery."""
    r = client.put(
        "/api/v1/system/provider-config/kimi_coding/default",
        json={"api_key": "sk-kimi-test-123"},  # pragma: allowlist secret
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text

    tokens = _cache_tokens("kimi_coding", "default")
    assert tokens["api_key"] == "sk-kimi-test-123"  # pragma: allowlist secret
    assert tokens["oauth_token"] == "sk-kimi-test-123"  # pragma: allowlist secret
    from app.services.token_cache import token_cache

    sources = token_cache._source_cache["kimi_coding"]["default"]
    config_source = sources["config:kimi_coding:default"]
    assert config_source[0]["api_key"] == "sk-kimi-test-123"  # pragma: allowlist secret
    assert config_source[1]["source_type"] == "config"


def test_kimi_api_key_per_account_put_mirrors_to_token_cache(client: TestClient):
    """Same mirror on the multi-account canonical endpoint, stamped under the
    canonical (lowercased) account_id so the collector's identity-scoped cache
    lookup lines up."""
    r = client.put(
        "/api/v1/system/provider-config/kimi_coding/Alice@Example.com",
        json={"api_key": "sk-kimi-alice-123"},  # pragma: allowlist secret
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text

    tokens = _cache_tokens("kimi_coding", "alice@example.com")
    assert tokens["api_key"] == "sk-kimi-alice-123"  # pragma: allowlist secret


def test_kimi_clear_api_key_drops_cache_mirror(client: TestClient):
    """clear_api_key must invalidate the mirrored slot too — a stale copy
    would keep feeding collectors a key the user just removed."""
    r = client.put(
        "/api/v1/system/provider-config/kimi_coding/default",
        json={"api_key": "sk-kimi-test-123"},  # pragma: allowlist secret
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    assert _cache_tokens("kimi_coding", "default")

    r = client.put(
        "/api/v1/system/provider-config/kimi_coding/default",
        json={"clear_api_key": True},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    assert not _cache_tokens("kimi_coding", "default")


def test_kimi_empty_string_api_key_clear_drops_cache_mirror(client: TestClient):
    """The documented empty-string clear (`api_key: ""` — the API/script
    path; the UI sends clear_api_key) must invalidate the mirror too, or
    collectors keep the removed key until its cache TTL expires."""
    r = client.put(
        "/api/v1/system/provider-config/kimi_coding/default",
        json={"api_key": "sk-kimi-test-123"},  # pragma: allowlist secret
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    assert _cache_tokens("kimi_coding", "default")

    r = client.put(
        "/api/v1/system/provider-config/kimi_coding/default",
        json={"api_key": ""},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    assert not _cache_tokens("kimi_coding", "default")


def test_empty_string_api_key_clear_preserves_other_cache_slots(client: TestClient):
    """opencode/ollama empty-string clears drop only the api_key family — a
    sidecar-pushed credential in another slot must survive (same shape as the
    clear_api_key flag, PR #287)."""
    import asyncio

    from app.services.token_cache import token_cache

    r = client.put(
        "/api/v1/system/provider-config/ollama/default",
        json={"api_key": "sk-ollama-123"},  # pragma: allowlist secret
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    seeded = _cache_tokens("ollama", "default")
    assert seeded["api_key"] == "sk-ollama-123"  # pragma: allowlist secret

    # A sidecar push lands an independent credential family for the same account.
    asyncio.run(
        token_cache.store(
            "ollama",
            {"session_cookie": "sidecar-cookie"},  # pragma: allowlist secret
            account_id="default",
            source="sidecar-a",
        )
    )

    r = client.put(
        "/api/v1/system/provider-config/ollama/default",
        json={"api_key": ""},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text

    tokens = _cache_tokens("ollama", "default")
    assert "api_key" not in tokens
    assert "oauth_token" not in tokens
    assert tokens.get("session_cookie") == "sidecar-cookie"  # pragma: allowlist secret


def test_clearing_pasted_key_keeps_sidecar_source_bundles(client: TestClient):
    """Clearing a dashboard-pasted key used to ``token_cache.remove`` the whole
    account, dropping every sidecar source bundle until each machine re-pushed."""
    import asyncio

    from app.services.token_cache import token_cache

    r = client.put(
        "/api/v1/system/provider-config/minimax/default",
        json={"api_key": "sk-minimax-123"},  # pragma: allowlist secret
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    asyncio.run(
        token_cache.store(
            "minimax",
            {"api_key": "sidecar-key"},  # pragma: allowlist secret
            account_id="default",
            source="sidecar-a",
            source_id="sidecar:host-a:auth",
        )
    )

    r = client.put(
        "/api/v1/system/provider-config/minimax/default",
        json={"clear_api_key": True},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text

    sources = {
        c["source_id"]: c["tokens"]
        for c in asyncio.run(token_cache.get_source_candidates("minimax", "default"))
    }
    sidecar_bundle = {"api_key": "sidecar-key"}  # pragma: allowlist secret
    assert sources == {"sidecar:host-a:auth": sidecar_bundle}
    assert "config:minimax:default" not in sources


def test_kimi_cache_mirror_resolves_through_real_collector(client: TestClient):
    """End-to-end (issue #343): PUT mirror → real ``_resolve_code_bearer``.

    Tiers 1-2 (DB/env) are neutralized so the assertion can only pass via
    the token-cache tier through the real store — pinning the writer/reader
    account-id contract that the mocked collector unit tests cannot see."""
    import asyncio
    from unittest.mock import patch as mock_patch

    from app.services.collectors.kimi_coding import KimiCodingCollector

    r = client.put(
        "/api/v1/system/provider-config/kimi_coding/default",
        json={"api_key": "sk-kimi-e2e-123"},  # pragma: allowlist secret
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text

    async def _resolve(account_id: str):
        collector = KimiCodingCollector(account_id=account_id)
        with (
            mock_patch("app.services.collectors.kimi_coding.credential_provider") as mock_cp,
            mock_patch("app.services.collectors.kimi_coding.settings") as mock_settings,
        ):
            mock_cp.get_provider_api_key.return_value = None
            mock_cp.get_provider_session_cookie.return_value = None
            mock_cp.get_credentials.return_value = {}
            mock_settings.KIMI_AUTH_TOKEN = ""
            mock_settings.KIMI_CODE_API_KEY = ""
            mock_settings.KIMI_CODE_BASE_URL = ""
            return await collector._resolve_code_bearer()

    resolved = asyncio.run(_resolve("default"))
    assert resolved is not None, "cached mirror did not resolve"
    token, input_source, is_cli = resolved
    assert token == "sk-kimi-e2e-123"  # pragma: allowlist secret
    assert input_source == "config"
    assert is_cli is False

    # A collector under a different identity still reaches the mirror when
    # its own cache slot is absent (get_with_metadata's "default" fallback).
    resolved = asyncio.run(_resolve("alice@example.com"))
    assert resolved is not None, "default-slot fallback did not resolve"
    assert resolved[0] == "sk-kimi-e2e-123"  # pragma: allowlist secret
    assert resolved[2] is False


def test_is_orphaned_true_when_default_shadowed_by_live_sibling(
    client: TestClient, session: Session
) -> None:
    """Default row + non-default sibling with live data: orphan flag fires."""
    # Two real rows: alice@example.com and default.
    for label, account_id in (("alice", "alice@example.com"), ("only", "default")):
        r = client.put(
            f"/api/v1/system/provider-config/openrouter/{account_id}",
            json={"account_label": label},
            headers=_admin_headers(),
        )
        assert r.status_code == 200, r.text

    # The sibling has live data; the default row does not.
    _seed_latest_usage(session, "openrouter", "alice@example.com")

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    assert by_id["default"]["is_orphaned"] is True, "default shadowed by live sibling"
    assert by_id["alice@example.com"]["is_orphaned"] is False, "sibling is the live one"


def test_is_orphaned_false_when_default_has_live_data(client: TestClient, session: Session) -> None:
    """Default row with live data + sibling without live data: NOT orphaned.

    The default row is the live one, so it isn't shadowed.
    """
    for label, account_id in (("alice", "alice@example.com"), ("default", "default")):
        r = client.put(
            f"/api/v1/system/provider-config/openrouter/{account_id}",
            json={"account_label": label},
            headers=_admin_headers(),
        )
        assert r.status_code == 200, r.text

    _seed_latest_usage(session, "openrouter", "default")

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    assert by_id["default"]["is_orphaned"] is False
    assert by_id["alice@example.com"]["is_orphaned"] is False


def test_clear_session_cookie_wipes_and_clears_oai_sc_companion(client: TestClient):
    """ChatGPT-specific: clearing the session_cookie also wipes the
    oai-sc companion field (which derives from the same paste)."""
    # Seed with a multi-cookie paste that yields both session_cookie and oai_sc.
    cookie_paste = (
        "__Secure-next-auth.session-token=eyJ.hbGc.payload; oai-sc=eyJzZWNyZXQ.b64.signature"
    )
    r = client.put(
        "/api/v1/system/provider-config/chatgpt/default",
        json={"session_cookie": cookie_paste, "account_label": "Default"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    r = client.put(
        "/api/v1/system/provider-config/chatgpt/default",
        json={"clear_session_cookie": True},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    chatgpt = next(p for p in listing if p["provider_id"] == "chatgpt")
    default = next(a for a in chatgpt["accounts"] if a["account_id"] == "default")
    assert default["session_cookie_set"] is False


def test_setting_api_key_while_clearing_cookie_keeps_same_account_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """The cookie clear must not evict an API key written in the same PUT."""

    async def no_sync(*args, **kwargs):
        return None

    async def no_collect(*args, **kwargs):
        return []

    from app.services.collector_manager import manager
    from app.services.token_cache import token_cache

    monkeypatch.setattr(manager, "_sync_collectors", no_sync)
    monkeypatch.setattr(manager, "collect_one", no_collect)

    r = client.put(
        "/api/v1/system/provider-config/opencode/alice@example.com",
        json={"api_key": "oc_sk_test", "clear_session_cookie": True},  # pragma: allowlist secret
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["account_id"] == "alice@example.com"
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    opencode = next(p for p in listing if p["provider_id"] == "opencode")
    assert [a["account_id"] for a in opencode["accounts"]] == ["alice@example.com"]
    account = opencode["accounts"][0]
    assert account["api_key_set"] is True
    assert account["session_cookie_set"] is False
    cached_types = token_cache._cache["opencode"]["alice@example.com"][0]
    assert any(token_type.endswith("_key") for token_type in cached_types)


def test_archived_account_does_not_inflate_active_account_count(client: TestClient):
    client.put(
        "/api/v1/system/provider-config/openrouter/old@example.com",
        json={"archived": True},
        headers=_admin_headers(),
    )
    client.put(
        "/api/v1/system/provider-config/openrouter/live@example.com",
        json={},
        headers=_admin_headers(),
    )
    provider = next(
        p
        for p in client.get("/api/v1/system/provider-configs").json()["providers"]
        if p["provider_id"] == "openrouter"
    )
    assert provider["account_count"] == 1
    assert provider["archived_count"] == 1
    assert {a["account_id"] for a in provider["accounts"]} == {
        "old@example.com",
        "live@example.com",
    }


def test_discovered_account_label_is_saved_independently(client: TestClient, session: Session):
    session.add(
        LatestUsage(
            provider_id="openrouter",
            account_id="opaque-account",
            card_json="{}",
        )
    )
    session.commit()
    response = client.put(
        "/api/v1/system/provider-account-label/openrouter/opaque-account",
        json={"account_label": "Work key"},
        headers=_admin_headers(),
    )
    assert response.status_code == 200, response.text

    provider = next(
        item
        for item in client.get("/api/v1/system/provider-configs").json()["providers"]
        if item["provider_id"] == "openrouter"
    )
    account = next(item for item in provider["accounts"] if item["account_id"] == "opaque-account")
    assert account["source"] == "discovered"
    assert account["account_label"] == "Work key"


def test_account_merge_requires_explicit_confirmation_for_gauge_collision(
    client: TestClient, session: Session
):
    session.add_all(
        [
            LatestUsage(
                provider_id="openrouter",
                account_id="source@example.com",
                window_type="weekly",
                variant="default",
                model_id="",
                card_json='{"remaining": 10}',
            ),
            LatestUsage(
                provider_id="openrouter",
                account_id="target@example.com",
                window_type="weekly",
                variant="default",
                model_id="",
                card_json='{"remaining": 8}',
            ),
        ]
    )
    session.commit()
    body = {
        "provider_id": "openrouter",
        "source_account_id": "source@example.com",
        "destination_account_id": "target@example.com",
    }
    preview = client.post(
        "/api/v1/system/provider-account-merge/preview", json=body, headers=_admin_headers()
    )
    assert preview.status_code == 200
    assert preview.json()["counts"]["latest_usage"]["collisions"] == 1

    apply_url = "/api/v1/system/provider-account-merge/apply"
    rejected = client.post(apply_url, json=body, headers=_admin_headers())
    assert rejected.status_code == 409

    applied = client.post(
        apply_url,
        json={**body, "confirm_collisions": True},
        headers=_admin_headers(),
    )
    assert applied.status_code == 200, applied.text
    rows = session.exec(select(LatestUsage).where(LatestUsage.provider_id == "openrouter")).all()
    assert [row.account_id for row in rows] == ["target@example.com"]


def test_account_merge_warns_when_default_is_destination(client: TestClient, session: Session):
    session.add_all(
        [
            LatestUsage(
                provider_id="openrouter",
                account_id="source@example.com",
                window_type="weekly",
                variant="source-series",
                card_json='{"remaining": 10}',
            ),
            LatestUsage(
                provider_id="openrouter",
                account_id="default",
                window_type="weekly",
                variant="default-series",
                card_json='{"remaining": 8}',
            ),
        ]
    )
    session.commit()
    body = {
        "provider_id": "openrouter",
        "source_account_id": "source@example.com",
        "destination_account_id": "default",
    }
    preview = client.post(
        "/api/v1/system/provider-account-merge/preview", json=body, headers=_admin_headers()
    )
    assert preview.status_code == 200
    assert preview.json()["shared_default_warning"] is True

    apply_url = "/api/v1/system/provider-account-merge/apply"
    rejected = client.post(apply_url, json=body, headers=_admin_headers())
    assert rejected.status_code == 409
    applied = client.post(
        apply_url,
        json={**body, "confirm_shared_default": True},
        headers=_admin_headers(),
    )
    assert applied.status_code == 200, applied.text


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


def test_archived_account_usage_flags_ignore_unrendered_provider_rows(
    session: Session, monkeypatch
):
    from app.api.endpoints.system import _archived_account_usage_flags
    from app.models.db import ProviderConfig
    from app.services.collector_manager import manager

    monkeypatch.setattr(manager, "collector_registry", {})
    row = ProviderConfig(
        provider_id="opencode-free", account_id="archived@example.com", archived=True
    )

    assert _archived_account_usage_flags(session, [row]) == {}


# ---------------------------------------------------------------------------
# preview_account_identity — edge cases (#287)
# ---------------------------------------------------------------------------


def test_preview_account_returns_default_when_credential_exists(client: TestClient):
    """Same body, but the canonical ``"default"`` row already exists —
    ``already_exists`` flips to True so the wizard can render the inline
    collision error before the user clicks Next."""
    # Seed a row under account_id="default" so the preview sees it.
    r = client.put(
        "/api/v1/system/provider-config/anthropic/default",
        json={"account_label": "Default"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "anthropic"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    assert r.json()["already_exists"] is True


def test_preview_account_falls_back_to_sha256_when_no_email_or_jwt(client: TestClient):
    """An opaque (non-email, non-JWT) credential hits the SHA hash fallback
    path in ``resolve_account_id`` — opaque session tokens end up here for
    providers like Anthropic that don't extract an email claim. Switched
    from SHA-256 to SHA-512 to clear CodeQL's
    ``py/weak-sensitive-data-hashing`` rule (which only flags SHA-1 /
    SHA-256 of password-tainted data); the 128-char hex output fits the
    existing ``account_id: str`` column with no length cap."""
    import hashlib

    opaque = "opaque-session-token-with-no-email-claim"  # pragma: allowlist secret
    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "anthropic", "session_cookie": opaque},
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["label_source"] == "credential_hash"
    assert (
        data["suggested_account_id"]
        == hashlib.pbkdf2_hmac("sha256", opaque.encode(), b"runway-account-id-v1", 1).hex()
    )


def test_preview_account_returns_404_for_unknown_provider(client: TestClient):
    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "totally-fake-provider",
            "api_key": "sk-x",  # pragma: allowlist secret
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 404
    assert "Unknown provider" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Cookie-paste extraction (#287) — system.py session_cookie parsing
# ---------------------------------------------------------------------------


def test_apply_provider_config_extracts_session_key_from_cookie_paste(client: TestClient):
    """Pasting a multi-cookie header that contains a ``sessionKey=...`` pair
    (without Cloudflare's cf_clearance present) causes the server to store
    just the sessionKey value, not the whole paste. The Anthropic collector
    reads the stored cookie directly, so the truncated value keeps it happy
    without leaking the unrelated cookie metadata.
    """
    cookie_paste = (
        "sessionKey=sk-ant-secret-value; other_cookie=irrelevant"  # pragma: allowlist secret
    )

    r = client.put(
        "/api/v1/system/provider-config/anthropic/default",
        json={"session_cookie": cookie_paste},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    # The row exists with session_cookie_set=True; the actual stored value is
    # not exposed via the API (it's encrypted), but the assert pins the
    # positive path: the request succeeds, the row materialises.
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    anthropic = next(p for p in listing if p["provider_id"] == "anthropic")
    default = next(a for a in anthropic["accounts"] if a["account_id"] == "default")
    assert default["session_cookie_set"] is True


# ---------------------------------------------------------------------------
# DELETE /provider-config/{provider_id}/{account_id}
# ---------------------------------------------------------------------------


def test_delete_provider_config_archives_row(client: TestClient, session: Session) -> None:
    """Per-account DELETE soft-archives the row + evicts LatestUsage cards.

    Pins the contract that the webapp's ProviderDetailDialog Remove action
    depends on — without it, the front-end surfaces a 'Method Not Allowed'
    toast (FastAPI 405). The endpoint evicts LatestUsage rows so the
    dashboard doesn't show ghost cards for an account the operator just
    removed, and clears the in-memory token_cache entry so collectors
    don't keep hitting the removed account's credentials.

    PR #317 round-2 review note: we soft-archive (``row.archived=True``,
    ``row.enabled=False``) rather than hard-delete because ``usage_events``
    for the pair survives the operation, and the synthetic loop in
    ``_fetch_fleet_view_sync`` (``app/api/endpoints/usage.py:240``) would
    re-create the card from those events if the row weren't in the
    ``archived_pairs`` skip-set. Soft-archive keeps the pair in the
    skip-set and the dashboard filters it out cleanly.
    """
    # Seed a row with a stored credential — the DELETE must wipe it
    # (PR #317 round-2 re-review warning: the dialog's confirm copy
    # promises "deletes the configuration row and its stored
    # credentials"; a kept credential could be re-cached on re-enable).
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={
            "account_label": "Alice",
            "api_key": "sk-or-test-alice",  # pragma: allowlist secret
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    # session_cookie isn't accepted for openrouter via PUT (no cookie
    # support), so set it + the ChatGPT oai-sc companion directly to pin
    # that branch of the wipe too (round-3 approve nit: companion parity).
    from app.models.db import ProviderConfig

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "openrouter",
            ProviderConfig.account_id == "alice@example.com",
        )
    ).one()
    assert row.api_key is not None
    row.session_cookie = "sessionKey=stale"  # pragma: allowlist secret
    row.oai_sc_cookie = "oai-sc=stale"  # pragma: allowlist secret
    session.add(row)
    session.commit()

    # Seed a LatestUsage card that should be evicted by the delete.
    _seed_latest_usage(session, "openrouter", "alice@example.com")
    assert (
        session.exec(
            select(LatestUsage).where(
                LatestUsage.provider_id == "openrouter",
                LatestUsage.account_id == "alice@example.com",
            )
        ).first()
        is not None
    )

    # Pre-seed the in-memory token cache so the DELETE's
    # ``await token_cache.remove(...)`` path is exercised. Use
    # ``seed_sync`` rather than ``store`` — awaiting ``store()`` from a
    # throwaway loop binds the cache's asyncio.Lock to that loop so
    # later TestClient requests can't acquire it. See the
    # ``test_get_provider_configs_merges_cache_seeded_accounts`` test
    # above for the same pattern.
    import time

    from app.services.token_cache import token_cache

    token_cache.seed_sync(
        "openrouter",
        "alice@example.com",
        {"api_key": "sk-or-test-alice"},  # pragma: allowlist secret
        {"account_label": "Alice", "source": "config"},
        time.time(),
    )
    assert token_cache._cache.get("openrouter", {}).get("alice@example.com") is not None

    # Delete.
    r = client.delete(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "deleted"
    assert body["provider_id"] == "openrouter"
    assert body["account_id"] == "alice@example.com"

    # Row is gone from the operator-facing listing (soft-archive hides it).
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    # PR #317 round-2 review: we soft-archive (``archived=True``) rather
    # than hard-delete — the repo convention keeps archived rows visible
    # in this listing so the operator can un-archive if they change
    # their mind. The synthetic loop in /usage/fleet filters via the
    # ``archived_pairs`` skip-set (app/api/endpoints/usage.py:236,260).
    assert "alice@example.com" in by_id
    assert by_id["alice@example.com"]["archived"] is True
    assert by_id["alice@example.com"]["enabled"] is False

    # PR #317 round-2 re-review warning: stored credentials are wiped on
    # Remove — the dialog's confirm copy promises it, and a kept blob
    # could be re-cached if the row were ever re-enabled.
    session.refresh(
        session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "openrouter",
                ProviderConfig.account_id == "alice@example.com",
            )
        ).one()
    )
    wiped = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "openrouter",
            ProviderConfig.account_id == "alice@example.com",
        )
    ).one()
    assert wiped.api_key is None, "DELETE must clear the stored api_key"
    assert wiped.session_cookie is None, "DELETE must clear the stored session_cookie"
    assert wiped.oai_sc_cookie is None, (
        "DELETE must clear the ChatGPT oai_sc companion cookie "
        "(parity with clear_session_cookie, round-3 approve nit)"
    )
    assert wiped.oai_sc_cookie_encrypted is None

    # LatestUsage card was evicted.
    assert (
        session.exec(
            select(LatestUsage).where(
                LatestUsage.provider_id == "openrouter",
                LatestUsage.account_id == "alice@example.com",
            )
        ).first()
        is None
    )

    # Token-cache entry was cleared — collectors won't keep fetching
    # credentials for the removed account.
    assert token_cache._cache.get("openrouter", {}).get("alice@example.com") is None

    # PR #317 round-2 review: even with usage_events still holding the
    # pair, _fetch_fleet_view_sync's synthetic loop must NOT re-create a
    # card — that's the whole reason we soft-archive instead of
    # hard-delete. Pin it end-to-end.
    from datetime import UTC, datetime

    from app.models.db import UsageEvent

    session.add(
        UsageEvent(
            provider_id="openrouter",
            account_id="alice@example.com",
            event_id="evt-still-in-history-1",
            kind="message",
            ts=datetime.now(UTC),
            session_id="sess-after-delete",
        )
    )
    session.commit()

    r = client.get("/api/v1/usage/fleet")
    fleet = r.json().get("fleet", r.json())  # accept both shapes
    if isinstance(fleet, dict) and "entries" in fleet:
        fleet_entries = fleet["entries"]
    else:
        fleet_entries = fleet
    archived_alive = [
        e
        for e in fleet_entries
        if e.get("provider_id") == "openrouter" and e.get("account_id") == "alice@example.com"
    ]
    assert archived_alive == [], f"archived pair resurfaced in fleet view: {archived_alive}"


def test_permanent_delete_removes_empty_archived_account_and_source_state(
    client: TestClient, session: Session, monkeypatch
):
    from app.models.db import LatestUsageContribution, ProviderConfig
    from app.services.collector_manager import manager
    from app.services.credential_tags import CredentialTagRepo
    from app.services.token_cache import token_cache

    account_id = "duplicate@example.com"
    row = ProviderConfig(
        provider_id="opencode",
        account_id=account_id,
        enabled=False,
        archived=True,
        account_label="Duplicate",
        api_key="oc_sk_duplicate",  # pragma: allowlist secret
    )
    session.add(row)
    session.add(
        LatestUsage(
            provider_id="opencode",
            account_id=account_id,
            sidecar_id="dev-01",
            window_type="weekly",
            variant="default",
            model_id="",
            card_json="{}",
        )
    )
    session.add(
        LatestUsageContribution(
            provider_id="opencode",
            account_id=account_id,
            source_id="collector:opencode",
            window_type="weekly",
            variant="default",
            model_id="",
            card_json="{}",
        )
    )
    session.add(
        CredentialSource(
            provider_id="opencode",
            account_id=account_id,
            source_id="sidecar:old-auth",
            source_type="file",
            source_label="auth.json",
            sidecar_id="dev-01",
        )
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin="path:/old/auth.json#123456789abc",
        account_id=account_id,
        sidecar_id="dev-01",
    )
    session.commit()
    token_cache.seed_sync(
        "opencode",
        account_id,
        {"api_key": "oc_sk_duplicate"},  # pragma: allowlist secret
        {"source": "sidecar"},
    )
    preferences = {("opencode", account_id): {"sidecar:old-auth": (True, 0)}}
    monkeypatch.setattr(manager, "_credential_source_preferences", preferences)

    response = client.delete(
        f"/api/v1/system/provider-config/opencode/{account_id}?permanent=true",
        headers=_admin_headers(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "permanently_deleted"
    assert (
        session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "opencode", ProviderConfig.account_id == account_id
            )
        ).first()
        is None
    )
    assert (
        session.exec(
            select(LatestUsage).where(
                LatestUsage.provider_id == "opencode", LatestUsage.account_id == account_id
            )
        ).first()
        is None
    )
    assert (
        session.exec(
            select(LatestUsageContribution).where(
                LatestUsageContribution.provider_id == "opencode",
                LatestUsageContribution.account_id == account_id,
            )
        ).first()
        is None
    )
    assert (
        session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == "opencode",
                CredentialSource.account_id == account_id,
            )
        ).first()
        is None
    )
    assert (
        CredentialTagRepo.get(
            session,
            provider_id="opencode",
            credential_origin="path:/old/auth.json#123456789abc",
            sidecar_id="dev-01",
        )
        is None
    )
    assert token_cache._cache.get("opencode", {}).get(account_id) is None
    assert ("opencode", account_id) not in manager._credential_source_preferences

    audit_entry = session.exec(
        select(AuditLog).where(
            AuditLog.action == "provider_config.purge",
            AuditLog.target_id == f"opencode/{account_id}",
        )
    ).one()
    assert json.loads(audit_entry.payload_json or "{}") == {
        "tags_cleared": 1,
        "permanent": True,
    }


def test_permanent_delete_rejects_open_code_account_with_tier_usage(
    client: TestClient, session: Session
):
    from datetime import UTC, datetime

    from app.models.db import ProviderConfig, UsageEvent

    account_id = "archived@example.com"
    session.add(
        ProviderConfig(provider_id="opencode", account_id=account_id, enabled=False, archived=True)
    )
    session.add(
        UsageEvent(
            provider_id="opencode-free",
            account_id=account_id,
            event_id="free-tier-history",
            kind="message",
            ts=datetime.now(UTC),
        )
    )
    session.commit()

    listed = client.get("/api/v1/system/provider-configs")
    opencode = next(p for p in listed.json()["providers"] if p["provider_id"] == "opencode")
    archived_account = next(a for a in opencode["accounts"] if a["account_id"] == account_id)
    assert archived_account["has_usage_events"] is True

    response = client.delete(
        f"/api/v1/system/provider-config/opencode/{account_id}?permanent=true",
        headers=_admin_headers(),
    )
    assert response.status_code == 409
    assert (
        session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "opencode", ProviderConfig.account_id == account_id
            )
        ).first()
        is not None
    )


def test_delete_provider_config_returns_404_for_unknown_provider(
    client: TestClient,
) -> None:
    """Unknown provider → 404, not 405."""
    r = client.delete(
        "/api/v1/system/provider-config/no-such-provider/alice@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 404
    assert "no-such-provider" in r.json()["detail"]


def test_delete_provider_config_returns_404_for_missing_row(
    client: TestClient,
) -> None:
    """Known provider but no row at this account_id → 404."""
    r = client.delete(
        "/api/v1/system/provider-config/openrouter/nonexistent@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 404
    assert "nonexistent@example.com" in r.json()["detail"]


def test_delete_provider_config_requires_admin_key(client: TestClient, monkeypatch) -> None:
    """With ADMIN_API_KEY configured and APP_HOST bound off-loopback,
    unauthenticated requests are rejected AND no mutation occurs.

    Pins the auth gate that ``require_admin_key`` enforces on every
    state-changing endpoint — without it, an attacker on the same host
    could delete arbitrary provider configs. The "no mutation on
    rejection" half follows the pattern at
    ``tests/integration/test_audit_log.py:124`` (Hermes suggestion #6).

    Patches via dotted paths into both ``app.core.config`` and
    ``app.core.security``: ``resolve_auth`` reads ``settings`` from the
    latter's module-level binding, which can diverge from
    ``app.core.config.settings`` after another test reloads the config
    module (PR #297 round-1 regression guard).
    """
    # Seed a row first (the test fixture leaves ADMIN_API_KEY unset, so the
    # admin gate is effectively a no-op — we use the unauth path).
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    # Flip auth on: bind off-loopback so the localhost trust bypass
    # doesn't mask the missing-X-Admin-Key rejection we want to assert.
    for dotted in (
        "app.core.config.settings",
        "app.core.security.settings",
    ):
        monkeypatch.setattr(f"{dotted}.ADMIN_API_KEY", "admin-secret")
        monkeypatch.setattr(f"{dotted}.APP_HOST", "0.0.0.0")

    r = client.delete(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
    )
    assert r.status_code in (401, 403), (
        f"unauthenticated DELETE must be rejected, got {r.status_code}"
    )

    # Pin: the rejected request must NOT archive the row.
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    assert "alice@example.com" in by_id
    assert by_id["alice@example.com"]["archived"] is False


def test_delete_provider_config_default_row_removes_orphan(
    client: TestClient, session: Session
) -> None:
    """The exact bug behind the user's 'Method Not Allowed' toast: deleting
    a default provider_config row that's been shadowed by a non-default
    sibling succeeds end-to-end and the orphan disappears from the listing.

    Uses openrouter because Gemini's collector mixin auto-stores a
    cookie-derived email in token_cache during the post-PUT force-sync,
    which would surface as a "discovered" account and pollute the count
    assertion. OpenRouter has no such mixin, so the listing mirrors
    exactly what we put in provider_configs."""
    # Two rows: a labeled one + a "default" row left over from initial setup.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, f"PUT alice failed: {r.text}"
    r = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"account_label": "Default"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, f"PUT default failed: {r.text}"

    # The default row is flagged orphan (sibling has live data).
    _seed_latest_usage(session, "openrouter", "alice@example.com")

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    assert by_id["default"]["is_orphaned"] is True

    # Delete the orphan.
    r = client.delete(
        "/api/v1/system/provider-config/openrouter/default",
        headers=_admin_headers(),
    )
    assert r.status_code == 200, f"DELETE failed: {r.text}"

    # Alice is still active; the default row is now soft-archived
    # (archived=True, enabled=False) per the repo's hide-from-dashboard
    # convention. The dashboard's /usage/fleet view filters archived
    # pairs via ``archived_pairs`` so the ghost card never re-appears.
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    assert by_id["alice@example.com"]["archived"] is False
    assert by_id["alice@example.com"]["enabled"] is True
    assert by_id["default"]["archived"] is True
    assert by_id["default"]["enabled"] is False


def test_delete_provider_config_clears_dangling_credential_tags(
    client: TestClient, session: Session
) -> None:
    """PR #317 round-2 review warning: hard-deleting a provider_configs row
    leaves ``credential_tags.account_id`` rows pointing at the removed
    account, which the sidecar picks up via ``list_pending_payload`` and
    re-asserts via ``/fleet/ingest``. The handler must clear any matching
    tags atomically with the row removal.

    Pins:
      - ``CredentialTagRepo.delete_by_account`` is called for the pair.
      - The row count is reported on the response payload so the operator
        sees what was cleaned up.
      - A second tag for the SAME provider but a DIFFERENT account stays
        untouched (the bulk delete must not over-reach).
    """
    from app.models.db import CredentialTag
    from app.services.credential_tags import CredentialTagRepo

    # Seed the provider_config + a few CredentialTag rows.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    CredentialTagRepo.set_tag(
        session,
        provider_id="openrouter",
        credential_origin="env:OPENROUTER_API_KEY",
        account_id="alice@example.com",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="openrouter",
        credential_origin="file:/etc/openrouter-cookie",
        account_id="alice@example.com",
    )
    # Tag for a different account on the same provider — must survive.
    CredentialTagRepo.set_tag(
        session,
        provider_id="openrouter",
        credential_origin="env:OPENROUTER_API_KEY_BOB",
        account_id="bob@example.com",
    )
    session.commit()

    before = session.exec(
        select(CredentialTag).where(
            CredentialTag.provider_id == "openrouter",
            CredentialTag.account_id == "alice@example.com",
        )
    ).all()
    assert len(before) == 2

    r = client.delete(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tags_cleared"] == 2

    # The alice tags are gone.
    after = session.exec(
        select(CredentialTag).where(
            CredentialTag.provider_id == "openrouter",
            CredentialTag.account_id == "alice@example.com",
        )
    ).all()
    assert after == []

    # The bob tag survives — bulk delete must not over-reach.
    survivor = session.exec(
        select(CredentialTag).where(
            CredentialTag.provider_id == "openrouter",
            CredentialTag.account_id == "bob@example.com",
        )
    ).first()
    assert survivor is not None
    assert survivor.credential_origin == "env:OPENROUTER_API_KEY_BOB"


def test_delete_provider_config_writes_audit_row(client: TestClient, session: Session) -> None:
    """PR #317 round-2 review suggestion: pin the audit-log row.

    Every other admin mutation has an audit assertion
    (tests/integration/test_audit_log.py:62-124); the new DELETE should
    too. Also pins the negative case — a 404 does NOT leave a row,
    matching ``test_failed_mutation_does_not_write_audit_row``.
    """
    from app.models.db import AuditLog

    def _rows() -> list[AuditLog]:
        return list(session.exec(select(AuditLog).order_by(AuditLog.ts)).all())

    # Seed.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    # 404 path: a request for a non-existent pair must NOT write an audit row.
    pre_404 = _rows()
    r = client.delete(
        "/api/v1/system/provider-config/openrouter/nonexistent@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 404
    assert _rows() == pre_404, "failed DELETE must not write an audit row"

    # 200 path: a successful DELETE writes exactly one row.
    r = client.delete(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    rows = _rows()
    assert len(rows) == 1
    assert rows[0].action == "provider_config.delete"
    assert rows[0].target_id == "openrouter/alice@example.com"
    # Composite-key payload schema documented in PR #317.
    assert rows[0].payload_json is not None
    assert "tags_cleared" in rows[0].payload_json


def test_delete_provider_config_drops_smart_collector(client: TestClient, session: Session) -> None:
    """PR #317 round-2 re-review suggestion: pin the ``_sync_collectors(force=True)``
    call with the manager's real pair key shape.

    Without the sync, ``manager.smart_collectors`` keeps a SmartCollector
    for the removed pair alive and the next poll re-writes a
    ``LatestUsage`` card, undoing the eviction. Round-2 review noted the
    earlier version registered under the bare provider id (``"openrouter"``),
    which passes via step 3's generic prune of any key not in
    ``active_keys`` — it pinned the presence of a sync call, not the pair
    semantics. This version:
      - registers under the real ``f"{pid}:{aid}"`` key,
      - seeds the token cache for the pair so before-DELETE the pair is
        genuinely active (cache entry + collector both present),
      - after DELETE asserts BOTH are gone (cache cleared by the handler,
        collector pruned because the pair left ``active_keys``),
      - restores the manager singleton so later tests see prior state.
    """
    import time

    from app.services.collector_manager import manager
    from app.services.smart_collector import SmartCollector
    from app.services.token_cache import token_cache

    pid = "openrouter"
    aid = "alice@example.com"
    pair_key = f"{pid}:{aid}"

    # Isolate the singleton: snapshot and restore around the test.
    saved_collectors = dict(manager.smart_collectors)
    try:
        # Seed config + token cache so the pair is a genuinely active
        # dynamic collector before the DELETE.
        r = client.put(
            f"/api/v1/system/provider-config/{pid}/{aid}",
            json={"account_label": "Alice"},
            headers=_admin_headers(),
        )
        assert r.status_code == 200

        token_cache.seed_sync(
            pid,
            aid,
            {"api_key": "sk-or-test-pair"},  # pragma: allowlist secret
            {"account_label": "Alice", "source": "config"},
            time.time(),
        )
        assert token_cache._cache.get(pid, {}).get(aid) is not None

        collector = SmartCollector.__new__(SmartCollector)
        collector.provider_id = pid
        collector.account_id = aid
        manager.smart_collectors[pair_key] = collector

        # Before DELETE: pair active in both cache and collectors.
        assert pair_key in manager.smart_collectors
        assert token_cache._cache.get(pid, {}).get(aid) is not None

        # Delete.
        r = client.delete(
            f"/api/v1/system/provider-config/{pid}/{aid}",
            headers=_admin_headers(),
        )
        assert r.status_code == 200

        # The DELETE handler must trigger a sync that prunes the removed
        # pair from manager.smart_collectors (it left active_keys once the
        # handler dropped its cache entry) — otherwise the next poll would
        # re-write a LatestUsage card and undo the eviction.
        assert pair_key not in manager.smart_collectors, (
            f"DELETE must drop the SmartCollector for the removed pair; "
            f"manager.smart_collectors={list(manager.smart_collectors)}"
        )
        # And the cache entry the pair was active on is gone.
        assert token_cache._cache.get(pid, {}).get(aid) is None
    finally:
        manager.smart_collectors.clear()
        manager.smart_collectors.update(saved_collectors)


def test_delete_provider_config_reenable_put_stays_disabled(
    client: TestClient, session: Session
) -> None:
    """PR #317 round-2 re-review warning: an archived row must never re-enable.

    The dialog's master toggle sends ``{"enabled": true}`` PUTs for every
    disabled account. On a Remove'd (archived) row that used to leave
    ``archived=True, enabled=True`` — the manual-cache sync gate
    (``collector_manager.py:113``) would re-cache the credential and
    step-2 would respawn a collector while ``archived_pairs`` kept hiding
    the pair from the fleet view: invisible collection.

    Pins the invariant ``row.archived ⇒ row.enabled is False`` enforced in
    ``_apply_provider_config_update``, and that an explicit un-archive
    (``archived: false``) is still the recovery path.
    """
    from app.models.db import ProviderConfig
    from app.services.token_cache import token_cache

    pid = "openrouter"
    aid = "alice@example.com"

    r = client.put(
        f"/api/v1/system/provider-config/{pid}/{aid}",
        json={
            "account_label": "Alice",
            "api_key": "sk-or-test-reenable",  # pragma: allowlist secret
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    r = client.delete(
        f"/api/v1/system/provider-config/{pid}/{aid}",
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    # Master-switch style enable PUT — the exact payload the dialog sends.
    r = client.put(
        f"/api/v1/system/provider-config/{pid}/{aid}",
        json={"enabled": True},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == pid,
            ProviderConfig.account_id == aid,
        )
    ).one()
    assert row.archived is True
    assert row.enabled is False, (
        "enabling an archived row must be a no-op (archived ⇒ enabled=False)"
    )
    # Credentials were wiped by DELETE and must not be re-cached by the
    # PUT's post-commit collector sync.
    assert token_cache._cache.get(pid, {}).get(aid) is None

    # Recovery path: explicit un-archive re-enables the row (existing
    # flow — ProviderPage archive toggle / settings dialog).
    r = client.put(
        f"/api/v1/system/provider-config/{pid}/{aid}",
        json={"archived": False},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    session.expire_all()
    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == pid,
            ProviderConfig.account_id == aid,
        )
    ).one()
    assert row.archived is False
    assert row.enabled is True


def test_only_a_credential_change_clears_the_auth_failure_flag(client: TestClient):
    """A label-only save must not dismiss a live "provider rejected it" flag."""
    from app.services import auth_failures

    url = "/api/v1/system/provider-config/openrouter/alice@example.com"
    auth_failures.mark("openrouter", "alice@example.com")

    r = client.put(url, json={"account_label": "Alice"}, headers=_admin_headers())
    assert r.status_code == 200, r.text
    r = client.put(url, json={"enabled": True}, headers=_admin_headers())
    assert r.status_code == 200, r.text
    assert auth_failures.flagged_accounts("openrouter") == {"alice@example.com"}

    r = client.put(
        url, json={"api_key": "sk-or-new"}, headers=_admin_headers()
    )  # pragma: allowlist secret
    assert r.status_code == 200, r.text
    assert auth_failures.flagged_accounts("openrouter") == set()


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
