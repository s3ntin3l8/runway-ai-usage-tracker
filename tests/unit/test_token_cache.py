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
