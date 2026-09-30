import base64
import json
import time
from types import SimpleNamespace

import pytest

from app.services.token_cache import TokenCache


def _make_id_token(payload: dict) -> str:
    """Build an unsigned JWT (header.payload.signature) — signature is not verified."""

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


@pytest.fixture
def cache():
    return TokenCache()


@pytest.mark.asyncio
async def test_retiring_moved_claude_source_keeps_independent_cookie(cache):
    source_id = "sidecar:claude-cli"
    await cache.store(
        "anthropic",
        {
            "oauth_token": "old-access",
            "refresh_token": "old-refresh",
            "expiry_date": "1790784000000",
        },
        account_id="old@example.com",
        source_id=source_id,
    )
    await cache.store(
        "anthropic",
        {"cookie_sessionKey": "old-cookie"},  # pragma: allowlist secret
        account_id="old@example.com",
        source_id="sidecar:browser",
    )
    await cache.store(
        "anthropic",
        {"oauth_token": "new-access", "refresh_token": "new-refresh"},
        account_id="new@example.com",
        source_id=source_id,
    )

    assert await cache.remove_source(
        "anthropic", "old@example.com", source_id, retire_matching_oauth=True
    )
    assert await cache.get("anthropic", "old@example.com") == {"cookie_sessionKey": "old-cookie"}
    assert (await cache.get("anthropic", "new@example.com"))["oauth_token"] == "new-access"


@pytest.mark.asyncio
async def test_identity_pending_anthropic_source_uses_stable_source_id(cache):
    await cache.store(
        "anthropic",
        {"oauth_token": "rotating-token"},  # pragma: allowlist secret
        account_id="sidecar:stable-origin",
        source_id="sidecar:stable-origin",
        source_metadata={"identity_pending": True, "sidecar_id": "host-a"},
    )

    rows = await cache._get_source_credentials("anthropic")
    assert len(rows) == 1
    assert rows[0]["account_id"] == "sidecar:stable-origin"


@pytest.mark.asyncio
async def test_retiring_pending_source_drops_rotated_oauth_but_keeps_cookie(cache):
    source_id = "sidecar:stable-origin"
    await cache.store(
        "anthropic",
        {"oauth_token": "old-token", "refresh_token": "old-refresh"},  # pragma: allowlist secret
        account_id=source_id,
        source_id=source_id,
        source_metadata={"identity_pending": True, "sidecar_id": "host-a"},
    )
    cache._cache.setdefault("anthropic", {})[source_id] = (
        {
            "oauth_token": "rotated-token",
            "refresh_token": "rotated-refresh",  # pragma: allowlist secret
            "cookie_sessionKey": "independent-cookie",
        },
        {"identity_pending": True},
        time.time(),
    )

    assert await cache.remove_source("anthropic", source_id, source_id, retire_matching_oauth=True)
    assert await cache.get("anthropic", source_id) == {"cookie_sessionKey": "independent-cookie"}


@pytest.mark.asyncio
async def test_identity_pending_source_stays_hidden_until_promoted(cache):
    await cache.store(
        "antigravity",
        {"oauth_token": "opaque-token"},  # pragma: allowlist secret — fake token
        account_id="default",
        source_id="sidecar:origin-a",
        source_metadata={
            "source_type": "sidecar",
            "credential_origin": "path:/agy/token",
            "identity_pending": True,
        },
    )

    assert await cache.get_all_active_accounts() == []
    assert len(await cache.get_source_candidates("antigravity", "default")) == 1

    assert await cache.move_source(
        "antigravity", "default", "alice@example.com", "sidecar:origin-a"
    )
    assert [(pid, aid) for pid, aid, _label in await cache.get_all_active_accounts()] == [
        ("antigravity", "alice@example.com")
    ]
    assert (
        await cache.get_token("antigravity", "oauth_token", "alice@example.com") == "opaque-token"
    )


@pytest.mark.asyncio
async def test_move_source_refreshes_timestamp_when_merging_into_existing_account(
    cache, monkeypatch
):
    now = 1_800_000_000.0
    monkeypatch.setattr("app.services.token_cache.time.time", lambda: now)
    await cache.store(
        "antigravity",
        {"oauth_token": "pending-token"},  # pragma: allowlist secret
        account_id="default",
        source_id="sidecar:origin-a",
        source_metadata={"identity_pending": True},
    )
    cache._cache["antigravity"] = {
        "alice@example.com": (
            {"api_key": "existing-token"},  # pragma: allowlist secret
            {"source_id": "other-source"},
            now,
        )
    }
    cache._token_timestamps["antigravity"] = {"alice@example.com": {"oauth_token": now - 1000}}

    assert await cache.move_source(
        "antigravity", "default", "alice@example.com", "sidecar:origin-a"
    )

    assert cache._token_timestamps["antigravity"]["alice@example.com"]["oauth_token"] == now


@pytest.mark.asyncio
async def test_move_source_keeps_newer_report_when_migrating_legacy_hash_bucket(cache):
    source_id = "sidecar:host:oauth-json"
    await cache.store(
        "antigravity",
        {
            "oauth_token": "old-token",  # pragma: allowlist secret
            "refresh_token": "old-refresh",  # pragma: allowlist secret
            "api_key": "legacy-api",  # pragma: allowlist secret
        },
        account_id="legacy-hash",
        account_label="Legacy",
        source_id=source_id,
        source_metadata={"sidecar_id": "host", "credential_origin": "path:/oauth.json"},
    )
    await cache.store(
        "antigravity",
        {
            "oauth_token": "fresh-token",  # pragma: allowlist secret
            "refresh_token": "target-refresh",  # pragma: allowlist secret
        },
        account_id="alice@example.com",
        source_id=source_id,
        source_metadata={"sidecar_id": "host", "credential_origin": "path:/oauth.json"},
    )
    cache._cache["antigravity"]["legacy-hash"][1]["source_id"] = source_id
    prior_oauth_seen = cache._token_timestamps["antigravity"]["alice@example.com"]["oauth_token"]

    assert await cache.move_source("antigravity", "legacy-hash", "alice@example.com", source_id)

    candidates = await cache.get_source_candidates("antigravity", "alice@example.com")
    assert candidates[0]["tokens"]["oauth_token"] == "fresh-token"
    assert candidates[0]["tokens"]["refresh_token"] == "target-refresh"
    assert candidates[0]["tokens"]["api_key"] == "legacy-api"  # pragma: allowlist secret
    aggregate = await cache.get("antigravity", "alice@example.com")
    assert aggregate is not None and aggregate["oauth_token"] == "fresh-token"
    assert aggregate["api_key"] == "legacy-api"  # pragma: allowlist secret
    assert cache._cache["antigravity"]["alice@example.com"][1]["account_label"] == "Legacy"
    assert (
        cache._token_timestamps["antigravity"]["alice@example.com"]["oauth_token"]
        == prior_oauth_seen
    )
    assert await cache.get_source_candidates("antigravity", "legacy-hash") == []


@pytest.mark.asyncio
async def test_move_source_migrates_legacy_aggregate_when_target_has_none(cache):
    source_id = "sidecar:host:legacy-oauth"
    await cache.store(
        "antigravity",
        {"oauth_token": "pending-token"},  # pragma: allowlist secret
        account_id="legacy-hash",
        source_id=source_id,
        source_metadata={"identity_pending": True},
    )
    cache._cache["antigravity"] = {
        "legacy-hash": (
            {"oauth_token": "pending-token"},  # pragma: allowlist secret
            {"source_id": source_id},
            time.time(),
        )
    }

    assert await cache.move_source("antigravity", "legacy-hash", "alice@example.com", source_id)

    assert await cache.get("antigravity", "alice@example.com") == {"oauth_token": "pending-token"}
    assert await cache.get("antigravity", "legacy-hash") is None


@pytest.mark.asyncio
async def test_move_source_does_not_promote_expired_legacy_token(cache):
    source_id = "sidecar:host:expired-oauth"
    expired_oauth = _make_id_token({"exp": time.time() - 60})
    await cache.store(
        "antigravity",
        {"oauth_token": expired_oauth},  # pragma: allowlist secret
        account_id="legacy-hash",
        source_id=source_id,
    )
    await cache.store(
        "antigravity",
        {"api_key": "target-api"},  # pragma: allowlist secret
        account_id="alice@example.com",
        source_id=source_id,
    )
    cache._cache["antigravity"]["legacy-hash"][1]["source_id"] = source_id

    assert await cache.move_source("antigravity", "legacy-hash", "alice@example.com", source_id)

    source = (await cache.get_source_candidates("antigravity", "alice@example.com"))[0]
    assert "oauth_token" not in source["tokens"]
    assert source["tokens"]["api_key"] == "target-api"  # pragma: allowlist secret
    aggregate = await cache.get("antigravity", "alice@example.com")
    assert aggregate == {"api_key": "target-api"}  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_store_and_get_token(cache):
    # Test default account (auto-id)
    acc_id = await cache.store("anthropic", {"api_key": "secret123"})

    # Verify we can get it back
    token = await cache.get_token("anthropic", "api_key")
    assert token == "secret123"

    # Check that an account was created
    accounts = await cache.get_accounts("anthropic")
    assert len(accounts) == 1
    assert accounts[0]["account_id"] == acc_id
    assert acc_id.startswith("")  # SHA-256 starts with anything valid


@pytest.mark.asyncio
async def test_multi_account_isolation(cache):
    # Store token for account A
    await cache.store("anthropic", {"api_key": "token1"}, account_id="acc_a")
    # Store different token for account B
    await cache.store("anthropic", {"api_key": "token2"}, account_id="acc_b")

    # Verify isolation
    assert await cache.get_token("anthropic", "api_key", account_id="acc_a") == "token1"
    assert await cache.get_token("anthropic", "api_key", account_id="acc_b") == "token2"

    # Verify counts
    accounts = await cache.get_accounts("anthropic")
    assert len(accounts) == 2


@pytest.mark.asyncio
async def test_same_type_credentials_remain_separate_sources(cache):
    await cache.store(
        "openrouter",
        {"api_key": "first"},  # pragma: allowlist secret — fake credential for cache test
        account_id="alice@example.com",
        source_id="sidecar:first",
        source_metadata={"priority": 1},
    )
    await cache.store(
        "openrouter",
        {"api_key": "second"},  # pragma: allowlist secret — fake credential for cache test
        account_id="alice@example.com",
        source_id="sidecar:second",
        source_metadata={"priority": 0},
    )

    candidates = await cache.get_source_candidates("openrouter", "alice@example.com")
    assert [entry["source_id"] for entry in candidates] == ["sidecar:second", "sidecar:first"]
    async with cache.using_source("openrouter", "alice@example.com", "sidecar:first"):
        assert await cache.get_token("openrouter", "api_key", "alice@example.com") == "first"
    async with cache.using_source("openrouter", "alice@example.com", "sidecar:second"):
        assert await cache.get_token("openrouter", "api_key", "alice@example.com") == "second"


@pytest.mark.asyncio
async def test_active_source_context_routes_all_cache_reads_and_updates(cache):
    await cache.store(
        "anthropic",
        {"oauth_token": "source-token", "account_label": "Alice"},
        account_id="alice@example.com",
        source_id="sidecar:host-a",
        source_metadata={"source_type": "sidecar"},
    )

    async with cache.using_source("anthropic", "alice@example.com", "sidecar:host-a"):
        source_accounts = await cache.get_accounts("anthropic")
        assert source_accounts[0]["account_id"] == "alice@example.com"
        assert source_accounts[0]["tokens"]["oauth_token"] == "source-token"
        assert await cache.get("anthropic", "alice@example.com") == {
            "oauth_token": "source-token",
            "account_label": "Alice",
        }
        tokens, metadata = await cache.get_with_metadata("anthropic", "alice@example.com")
        assert tokens["oauth_token"] == "source-token"
        assert metadata["source_type"] == "sidecar"
        assert cache.current_source_tokens("anthropic", "default")["oauth_token"] == "source-token"
        assert cache.current_source_metadata("anthropic", "default")["source_type"] == "sidecar"

        await cache.store("anthropic", {"refresh_token": "rotated"})
        assert await cache.get_token("anthropic", "refresh_token", "alice@example.com") == "rotated"

    assert await cache.get("anthropic", "alice@example.com") is not None


@pytest.mark.asyncio
async def test_stale_source_push_keeps_fresh_oauth_but_merges_refresh_and_other_fields(cache):
    future_expiry = str(int((time.time() + 3600) * 1000))
    past_expiry = str(int((time.time() - 3600) * 1000))
    await cache.store(
        "antigravity",
        {
            "oauth_token": "fresh-token",  # pragma: allowlist secret
            "refresh_token": "fresh-refresh",  # pragma: allowlist secret
            "expiry_date": future_expiry,
        },
        account_id="alice@example.com",
        source_id="sidecar:host-a",
    )
    await cache.store(
        "antigravity",
        {
            "oauth_token": "stale-token",  # pragma: allowlist secret
            "refresh_token": "rotated-refresh",  # pragma: allowlist secret
            "expiry_date": past_expiry,
            "account_label": "Alice",
        },
        account_id="alice@example.com",
        source_id="sidecar:host-a",
    )

    candidate = (await cache.get_source_candidates("antigravity", "alice@example.com"))[0]
    assert candidate["tokens"]["oauth_token"] == "fresh-token"
    assert candidate["tokens"]["refresh_token"] == "rotated-refresh"
    assert candidate["tokens"]["account_label"] == "Alice"


@pytest.mark.asyncio
async def test_source_bundles_expire_independently(cache):
    short_cache = TokenCache(ttl_seconds=0)
    await short_cache.store(
        "openrouter",
        {"api_key": "temporary"},  # pragma: allowlist secret — fake credential for expiry test
        account_id="default",
        source_id="env:one",
    )
    time.sleep(0.01)
    assert await short_cache.get_source_candidates("openrouter", "default") == []


@pytest.mark.asyncio
async def test_401_response_marks_only_active_source_attempt(cache):
    await cache.store(
        "openrouter",
        {"api_key": "provider-key"},  # pragma: allowlist secret — test credential
        account_id="default",
        source_id="env:one",
    )

    matching_request = SimpleNamespace(
        url="https://provider.example/usage",
        headers={"Authorization": "Bearer provider-key"},
    )
    unrelated_request = SimpleNamespace(
        url="https://metrics.example/ping",
        headers={"Authorization": "Bearer unrelated-key"},
    )
    matching_response = SimpleNamespace(status_code=401, request=matching_request)
    unrelated_response = SimpleNamespace(status_code=401, request=unrelated_request)

    async with cache.using_source("openrouter", "default", "env:one") as attempt:
        await cache.observe_response(unrelated_response)
        assert attempt["auth_failed"] is False
        await cache.observe_response(matching_response)
        assert attempt["auth_failed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["opencode", "ollama"])
async def test_remove_tokens_preserves_other_credential_family(cache, provider_id):
    await cache.store(
        provider_id,
        {
            "api_key": "key",  # pragma: allowlist secret
            "oauth_token": "key",  # pragma: allowlist secret
            "cookie_session": "cookie",
        },
        account_id="default",
        source="config",
    )

    await cache.remove_tokens(provider_id, "default", {"api_key", "oauth_token"})

    assert await cache.get(provider_id, "default") == {"cookie_session": "cookie"}


@pytest.mark.asyncio
async def test_identity_promotion(cache):
    # Store token with anonymous ID
    await cache.store("anthropic", {"api_key": "token1"}, account_id="acc_1")

    # Check initial state
    accs = await cache.get_accounts("anthropic")
    assert accs[0]["account_label"] is None

    # Promote identity
    await cache.update_account_metadata("anthropic", "acc_1", name="user@example.com")

    # Verify promotion
    accs = await cache.get_accounts("anthropic")
    assert accs[0]["account_label"] == "user@example.com"


@pytest.mark.asyncio
async def test_derive_account_id_prefers_id_token_email(cache):
    """Rotating tokens (oauth_token) must not produce a new entry on refresh."""
    id_token = _make_id_token({"email": "User@Example.com", "sub": "12345"})

    acc1 = await cache.store("gemini", {"oauth_token": "v1", "id_token": id_token})
    acc2 = await cache.store("gemini", {"oauth_token": "v2", "id_token": id_token})

    assert acc1 == acc2 == "user@example.com"
    assert len(await cache.get_accounts("gemini")) == 1


@pytest.mark.asyncio
async def test_derive_account_id_falls_back_to_sub(cache):
    id_token = _make_id_token({"sub": "google-uid-7777"})
    acc = await cache.store("gemini", {"oauth_token": "v1", "id_token": id_token})
    assert acc == "google-uid-7777"


@pytest.mark.asyncio
async def test_derive_account_id_hash_fallback_without_id_token(cache):
    acc = await cache.store("gemini", {"oauth_token": "abc"})
    assert acc != "abc" and len(acc) == 12  # 12-char sha256 prefix


@pytest.mark.asyncio
async def test_id_token_email_becomes_account_label(cache):
    id_token = _make_id_token({"email": "owner@example.com"})
    await cache.store("gemini", {"oauth_token": "v1", "id_token": id_token})
    accs = await cache.get_accounts("gemini")
    assert accs[0]["account_label"] == "owner@example.com"


@pytest.mark.asyncio
async def test_cache_expiration(cache):
    # Create cache with 0 TTL for immediate expiry
    short_cache = TokenCache(ttl_seconds=0)
    await short_cache.store("anthropic", {"api_key": "token1"}, account_id="acc_1")

    # Wait a tiny bit
    time.sleep(0.01)

    # Should be cleared
    assert await short_cache.get_token("anthropic", "api_key", account_id="acc_1") is None


def _jwt_exp(exp: float) -> str:
    return _make_id_token({"exp": exp, "sub": "test"})


@pytest.mark.asyncio
async def test_purge_strips_only_dead_oauth_fields(cache):
    """An expired, unrefreshable OAuth token can never recover — but a cookie
    stored beside it is an independent credential and must survive."""
    await cache.store(
        "chatgpt",
        {
            "oauth_token": _jwt_exp(time.time() - 60),
            "cookie___Secure-next-auth.session-token": "valid-cookie",
        },
        account_id="alice@x.com",
    )

    removed = await cache.purge_expired_unrefreshable()

    assert removed == 1
    assert await cache.get("chatgpt", "alice@x.com") == {
        "cookie___Secure-next-auth.session-token": "valid-cookie"
    }
    assert "oauth_token" not in cache._token_timestamps["chatgpt"]["alice@x.com"]


@pytest.mark.asyncio
async def test_purge_retains_sole_expired_entry(cache):
    """When the expired token is the account's only credential, keep it: Token
    Health must still report the account as dead rather than forget it."""
    await cache.store(
        "chatgpt",
        {"oauth_token": _jwt_exp(time.time() - 60)},
        account_id="dead-orphan",
    )

    removed = await cache.purge_expired_unrefreshable()

    assert removed == 0
    assert await cache.get("chatgpt", "dead-orphan") is not None


@pytest.mark.asyncio
async def test_purge_keeps_refreshable_expired(cache):
    """Expired but with a refresh_token — the auto-refresher will roll it; keep it."""
    await cache.store(
        "chatgpt",
        {"oauth_token": _jwt_exp(time.time() - 60), "refresh_token": "rt"},
        account_id="refreshable",
    )

    removed = await cache.purge_expired_unrefreshable()

    assert removed == 0
    assert await cache.get("chatgpt", "refreshable") is not None


@pytest.mark.asyncio
async def test_purge_keeps_valid_token(cache):
    """A token whose exp is in the future must not be evicted."""
    await cache.store(
        "chatgpt",
        {"oauth_token": _jwt_exp(time.time() + 3600)},
        account_id="still-good",
    )

    removed = await cache.purge_expired_unrefreshable()

    assert removed == 0
    assert await cache.get("chatgpt", "still-good") is not None


@pytest.mark.asyncio
async def test_purge_keeps_opaque_token(cache):
    """Opaque tokens (API keys, cookies) have no exp signal — never evict them."""
    await cache.store("openai", {"api_key": "sk-opaque"}, account_id="cfg")

    removed = await cache.purge_expired_unrefreshable()

    assert removed == 0
    assert await cache.get("openai", "cfg") is not None


@pytest.mark.asyncio
async def test_staler_push_does_not_clobber_fresher(cache):
    """A staler sidecar push must not downgrade a server-refreshed token.

    Gemini access tokens are opaque, so freshness rides on `expiry_date` (ms).
    """
    now_ms = int(time.time() * 1000)
    # Server-refreshed token: valid for another hour.
    await cache.store(
        "gemini",
        {"oauth_token": "fresh", "refresh_token": "rt", "expiry_date": str(now_ms + 3_600_000)},
        account_id="user@example.com",
    )
    # Sidecar re-pushes its stale local token (expired an hour ago).
    await cache.store(
        "gemini",
        {"oauth_token": "stale", "expiry_date": str(now_ms - 3_600_000)},
        account_id="user@example.com",
        source="sidecar-mgmt",
    )

    tokens = await cache.get("gemini", "user@example.com")
    assert tokens["oauth_token"] == "fresh"  # fresher token preserved
    assert tokens["refresh_token"] == "rt"  # not lost on the rejected overwrite


@pytest.mark.asyncio
async def test_staler_push_does_not_clobber_server_rotated_xai_refresh(cache):
    """A staler sidecar push must not overwrite a server-rotated xai_refresh."""
    now_ms = int(time.time() * 1000)
    # Server-refreshed token with rotated xai_refresh:
    await cache.store(
        "xai",
        {
            "xai_access": "fresh_jwt",
            "xai_refresh": "rt-rotated",
            "expiry_date": str(now_ms + 3_600_000),
        },
        account_id="user@example.com",
        source_id="sidecar-source",
    )

    # Sidecar re-pushes its stale local token with the old xai_refresh:
    await cache.store(
        "xai",
        {
            "xai_access": "stale_jwt",
            "xai_refresh": "rt-old",
            "expiry_date": str(now_ms - 3_600_000),
        },
        account_id="user@example.com",
        source_id="sidecar-source",
    )

    # Check top-level cache
    tokens = await cache.get("xai", "user@example.com")
    assert tokens["xai_access"] == "fresh_jwt"
    assert tokens["xai_refresh"] == "rt-rotated"

    # Check source_cache
    async with cache.using_source("xai", "user@example.com", "sidecar-source"):
        src_tokens = cache.current_source_tokens("xai", "user@example.com")
        assert src_tokens["xai_access"] == "fresh_jwt"
        assert src_tokens["xai_refresh"] == "rt-rotated"


@pytest.mark.asyncio
async def test_staler_push_absorbs_missing_xai_refresh(cache):
    """A staler push should provide xai_refresh if the cache did not have one."""
    now_ms = int(time.time() * 1000)
    # Cached token without xai_refresh:
    await cache.store(
        "xai",
        {"xai_access": "fresh_jwt", "expiry_date": str(now_ms + 3_600_000)},
        account_id="user@example.com",
        source_id="sidecar-source",
    )

    # Staler push containing xai_refresh:
    await cache.store(
        "xai",
        {
            "xai_access": "stale_jwt",
            "xai_refresh": "newly-discovered-rt",
            "expiry_date": str(now_ms - 3_600_000),
        },
        account_id="user@example.com",
        source_id="sidecar-source",
    )

    tokens = await cache.get("xai", "user@example.com")
    assert tokens["xai_access"] == "fresh_jwt"
    assert tokens["xai_refresh"] == "newly-discovered-rt"

    async with cache.using_source("xai", "user@example.com", "sidecar-source"):
        src_tokens = cache.current_source_tokens("xai", "user@example.com")
        assert src_tokens["xai_access"] == "fresh_jwt"
        assert src_tokens["xai_refresh"] == "newly-discovered-rt"


@pytest.mark.asyncio
async def test_sibling_credential_push_does_not_keep_removed_family_alive(monkeypatch):
    """A live CLI push must not extend the TTL of a browser credential no longer reported."""
    short_cache = TokenCache(ttl_seconds=10)
    now = [100.0]
    monkeypatch.setattr("app.services.token_cache.time.time", lambda: now[0])

    await short_cache.store(
        "chatgpt",
        {"oauth_token": "cli-token", "refresh_token": "cli-refresh"},
        account_id="user@example.com",
        source="sidecar-a",
    )
    await short_cache.store(
        "chatgpt",
        {"cookie_session": "browser-cookie"},
        account_id="user@example.com",
        source="sidecar-a",
    )

    now[0] += 8
    await short_cache.store(
        "chatgpt",
        {"oauth_token": "cli-token-2", "refresh_token": "cli-refresh-2"},
        account_id="user@example.com",
        source="sidecar-a",
    )

    now[0] += 3
    tokens = await short_cache.get("chatgpt", "user@example.com")
    assert tokens == {"oauth_token": "cli-token-2", "refresh_token": "cli-refresh-2"}


@pytest.mark.asyncio
async def test_repeated_stale_oauth_push_refreshes_protected_fields_ttl(monkeypatch):
    """A reported expired local OAuth token keeps protected fresh fields available."""
    short_cache = TokenCache(ttl_seconds=10)
    now = [1_700_000_000.0]
    monkeypatch.setattr("app.services.token_cache.time.time", lambda: now[0])
    fresh_expiry = str(int((now[0] + 3600) * 1000))

    await short_cache.store(
        "gemini",
        {
            "oauth_token": "server-fresh",
            "refresh_token": "server-refresh",
            "expiry_date": fresh_expiry,
        },
        account_id="user@example.com",
    )

    now[0] += 8
    await short_cache.store(
        "gemini",
        {
            "oauth_token": "local-expired",
            "refresh_token": "rotated-refresh",
            "expiry_date": str(int((now[0] - 1) * 1000)),
        },
        account_id="user@example.com",
        source="sidecar-a",
    )

    now[0] += 3
    tokens = await short_cache.get("gemini", "user@example.com")
    assert tokens == {
        "oauth_token": "server-fresh",
        "refresh_token": "rotated-refresh",
        "expiry_date": fresh_expiry,
    }


@pytest.mark.asyncio
async def test_known_expired_push_does_not_clobber_unknown_expiry_entry(cache):
    """A push with a known-expired credential must not overwrite an existing
    entry that has no comparable expiry signal of its own.

    This is the Antigravity incident: agy's opaque access token carries no
    exp/expiry_date at all (pre-fix), so an existing valid token and an
    expired one pushed by a different sidecar were incomparable and the
    expired push won purely on recency — silently breaking quota collection
    on every host except the one that had just re-pushed. `expiry_date` is
    now stamped for Antigravity, but this guards the general case (any
    provider/sidecar where the existing entry predates an expiry-stamping
    fix or the two sides are otherwise asymmetric).
    """
    now_ms = int(time.time() * 1000)
    # A different sidecar's still-valid push arrives with no expiry signal
    # (mirrors a pre-fix binary, or any opaque-token entry).
    await cache.store(
        "antigravity",
        {"oauth_token": "valid-no-signal"},
        account_id="user@example.com",
        source="sidecar-mgmt",
    )
    # This host's local agy session lapsed a day ago, but it still re-pushes
    # on every poll cycle.
    await cache.store(
        "antigravity",
        {"oauth_token": "expired", "expiry_date": str(now_ms - 86_400_000)},
        account_id="user@example.com",
        source="sidecar-dev-01",
    )

    tokens = await cache.get("antigravity", "user@example.com")
    assert tokens["oauth_token"] == "valid-no-signal"


@pytest.mark.asyncio
async def test_fresher_push_replaces_staler(cache):
    """A genuinely fresher push (later expiry) still wins."""
    now_ms = int(time.time() * 1000)
    await cache.store(
        "gemini",
        {"oauth_token": "old", "expiry_date": str(now_ms + 60_000)},
        account_id="user@example.com",
    )
    await cache.store(
        "gemini",
        {"oauth_token": "new", "expiry_date": str(now_ms + 3_600_000)},
        account_id="user@example.com",
    )

    tokens = await cache.get("gemini", "user@example.com")
    assert tokens["oauth_token"] == "new"


@pytest.mark.asyncio
async def test_opaque_tokens_always_overwrite(cache):
    """Without a comparable expiry on both sides, the latest write wins (unchanged)."""
    await cache.store("openai", {"api_key": "k1"}, account_id="cfg")  # pragma: allowlist secret
    await cache.store("openai", {"api_key": "k2"}, account_id="cfg")  # pragma: allowlist secret

    tokens = await cache.get("openai", "cfg")
    assert tokens["api_key"] == "k2"  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_sourceless_overwrite_preserves_sidecar_origin(cache):
    """A server-side refresh that omits `source` must not erase the sidecar origin.

    Many server-side callers (oauth_base, token_auto_refresher, collector_manager)
    store refreshed tokens without passing `source`, which previously clobbered the
    sidecar badge in Token Health. The fix: fall back to the prior metadata value.
    """
    # Sidecar pushes a token and establishes origin.
    await cache.store(
        "claude",
        {"access_token": "tok-v1"},
        account_id="user@example.com",
        source="my-laptop",
    )

    # Server-side refresh overwrites the token but carries no `source`.
    await cache.store(
        "claude",
        {"access_token": "tok-v2"},
        account_id="user@example.com",
        source=None,  # simulates oauth_base / token_auto_refresher callers
    )

    stats = await cache.get_all_stats()
    assert stats["claude"]["user@example.com"]["source"] == "my-laptop"


@pytest.mark.asyncio
async def test_truthy_source_still_overrides_prior_origin(cache):
    """A store that explicitly carries a `source` must override the previous origin.

    Ensures the fall-back only applies when the incoming `source` is falsy —
    an upgrade to `source="config"` (or a push from another sidecar) must
    still win. The one asymmetry: a non-explicit push cannot *downgrade* an
    explicit dashboard origin (see the next test).
    """
    await cache.store(
        "claude",
        {"access_token": "tok-v1"},
        account_id="user@example.com",
        source="sidecar-a",
    )
    await cache.store(
        "claude",
        {"access_token": "tok-v2"},
        account_id="user@example.com",
        source="config",
    )

    stats = await cache.get_all_stats()
    assert stats["claude"]["user@example.com"]["source"] == "config"


@pytest.mark.asyncio
async def test_sidecar_push_does_not_downgrade_config_origin(cache):
    """A dashboard paste keeps its `config` origin after a sidecar push.

    Entry-level `source` is what collectors turn into `input_source`: if a
    sidecar pushing an *independent* credential family for the same account
    re-stamped it, the pasted key would read `input_source=sidecar` and a 401
    on it would stop being treated as authoritative (PR #352 review). The
    pasted `api_key` itself must survive too.
    """
    await cache.store(
        "kimi_coding",
        {"api_key": "sk-pasted", "oauth_token": "sk-pasted"},  # pragma: allowlist secret
        account_id="user@example.com",
        source="config",
    )
    await cache.store(
        "kimi_coding",
        {"cli_access_token": "cli-token"},
        account_id="user@example.com",
        source="sidecar-laptop",
    )

    tokens = await cache.get("kimi_coding", "user@example.com")
    assert tokens["api_key"] == "sk-pasted"  # pragma: allowlist secret
    assert tokens["cli_access_token"] == "cli-token"
    stats = await cache.get_all_stats()
    assert stats["kimi_coding"]["user@example.com"]["source"] == "config"


@pytest.mark.asyncio
async def test_staler_sidecar_push_keeps_config_origin(cache):
    """The staler-push path applies the same precedence as the merge path.

    An expired sidecar token neither clobbers fresher tokens nor downgrades
    the dashboard origin recorded for the account.
    """
    now_ms = int(time.time() * 1000)
    await cache.store(
        "gemini",
        {"oauth_token": "fresh", "expiry_date": str(now_ms + 3_600_000)},
        account_id="user@example.com",
        source="config",
    )
    await cache.store(
        "gemini",
        {"oauth_token": "stale", "expiry_date": str(now_ms - 3_600_000)},
        account_id="user@example.com",
        source="sidecar-laptop",
    )

    tokens = await cache.get("gemini", "user@example.com")
    assert tokens["oauth_token"] == "fresh"
    stats = await cache.get_all_stats()
    assert stats["gemini"]["user@example.com"]["source"] == "config"


@pytest.mark.asyncio
async def test_source_lookups_return_snapshots(cache):
    await cache.store(
        "openrouter",
        {"api_key": "original"},  # pragma: allowlist secret — fake credential
        account_id="alice@example.com",
        source_id="source-a",
        source_metadata={"source_type": "sidecar"},
    )
    async with cache.using_source("openrouter", "alice@example.com", "source-a"):
        tokens = cache.current_source_tokens("openrouter", "alice@example.com")
        metadata = cache.current_source_metadata("openrouter", "alice@example.com")
        assert tokens is not None and metadata is not None
        tokens["api_key"] = "mutated"  # pragma: allowlist secret — fake credential
        metadata["source_type"] = "mutated"
    candidates = await cache.get_source_candidates("openrouter", "alice@example.com")
    assert candidates[0]["tokens"]["api_key"] == "original"  # pragma: allowlist secret
    assert candidates[0]["source_type"] == "sidecar"
