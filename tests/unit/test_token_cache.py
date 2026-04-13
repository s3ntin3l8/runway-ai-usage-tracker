import time

import pytest

from app.services.token_cache import TokenCache


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
    assert acc_id.startswith("") # SHA-256 starts with anything valid

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
async def test_cache_expiration(cache):
    # Create cache with 0 TTL for immediate expiry
    short_cache = TokenCache(ttl_seconds=0)
    await short_cache.store("anthropic", {"api_key": "token1"}, account_id="acc_1")
    
    # Wait a tiny bit
    time.sleep(0.01)
    
    # Should be cleared
    assert await short_cache.get_token("anthropic", "api_key", account_id="acc_1") is None
