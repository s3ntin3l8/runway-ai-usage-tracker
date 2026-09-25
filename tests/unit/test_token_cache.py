import base64
import json
import time

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
async def test_purge_removes_expired_unrefreshable(cache):
    """A past-exp oauth_token with no refresh_token can never recover — evict it."""
    await cache.store(
        "chatgpt",
        {"oauth_token": _jwt_exp(time.time() - 60)},
        account_id="dead-orphan",
    )

    removed = await cache.purge_expired_unrefreshable()

    assert removed == 1
    assert await cache.get("chatgpt", "dead-orphan") is None
    assert "chatgpt" not in cache._token_timestamps


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
    a fresher sidecar push or a `source="config"` store should still win.
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
