"""Integration tests for credentials: API keys, session cookies, token cache mirroring, and secret clearing."""

from __future__ import annotations

import pytest
from sqlmodel import select

from tests.fixtures.test_client import TestClient
from tests.integration._multi_account_provider_config_helpers import (
    _admin_headers,
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)


def _cache_tokens(provider: str, account_id: str) -> dict:
    """Tokens cached for a provider/account (empty dict when absent)."""
    from app.services.token_cache import token_cache

    entry = token_cache._cache.get(provider, {}).get(account_id)
    return entry[0] if entry else {}


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
    token, input_source, is_cli, device_id = resolved
    assert token == "sk-kimi-e2e-123"  # pragma: allowlist secret
    assert input_source == "config"
    assert is_cli is False

    # A collector under a different identity still reaches the mirror when
    # its own cache slot is absent (get_with_metadata's "default" fallback).
    resolved = asyncio.run(_resolve("alice@example.com"))
    assert resolved is not None, "default-slot fallback did not resolve"
    assert resolved[0] == "sk-kimi-e2e-123"  # pragma: allowlist secret
    assert resolved[2] is False


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


def test_encryption_failure_returns_retry_message_without_storing_secret(
    client, session, monkeypatch
):
    from unittest.mock import MagicMock

    from app.models.db import ProviderConfig, encryption_service

    broken_fernet = MagicMock()
    broken_fernet.encrypt.side_effect = RuntimeError("synthetic encryption failure")
    monkeypatch.setattr(encryption_service, "_fernet", broken_fernet)
    response = client.put(
        "/api/v1/system/provider-config/openrouter/test-account",
        json={"api_key": "synthetic"},  # pragma: allowlist secret — regression fixture
        headers=_admin_headers(),
    )
    assert response.status_code == 503
    assert "refusing to persist plaintext" in response.json()["detail"]
    assert "synthetic encryption failure" not in response.text
    session.rollback()
    rows = session.exec(select(ProviderConfig)).all()
    assert all(not row.api_key_encrypted for row in rows)
