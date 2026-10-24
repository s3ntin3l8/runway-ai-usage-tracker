"""A diagnostic snapshot must not reload, expire or combine credentials."""

import time

import pytest

from app.services.token_cache import TokenCache


@pytest.fixture(autouse=True)
def no_durable_reload(monkeypatch):
    monkeypatch.setattr("app.services.source_controls._ENABLED", False)


@pytest.mark.asyncio
async def test_snapshot_is_exact_and_returns_independent_copies():
    cache = TokenCache()
    await cache.store(
        "anthropic",
        {"oauth_token": "fixture"},
        account_id="one",
        source_id="a",
        source_metadata={"credential_origin": "file:/fixture/.credentials.json"},
    )
    tokens, metadata = await cache.peek_source("anthropic", "one", "a")
    tokens["oauth_token"] = "changed"
    metadata["credential_origin"] = "changed"
    actual = await cache.peek_source("anthropic", "one", "a")
    assert actual[0]["oauth_token"] == "fixture"
    assert actual[1]["credential_origin"] == "file:/fixture/.credentials.json"
    assert await cache.peek_source("anthropic", "other", "a") is None
    assert await cache.peek_source("anthropic", "one", "other") is None


@pytest.mark.asyncio
async def test_snapshot_has_no_merged_or_default_fallback():
    cache = TokenCache()
    await cache.store("anthropic", {"oauth_token": "merged"}, account_id="one")
    await cache.store(
        "anthropic", {"oauth_token": "default-source"}, account_id="default", source_id="a"
    )
    assert await cache.peek_source("anthropic", "one", "a") is None
    assert await cache.peek_source("anthropic", "one", "merged") is None


@pytest.mark.asyncio
async def test_snapshot_preserves_legacy_ttl_without_deleting_or_reloading(monkeypatch):
    cache = TokenCache(ttl_seconds=1)
    await cache.store("anthropic", {"oauth_token": "fixture"}, account_id="one", source_id="a")
    tokens, metadata, _ = cache._source_cache["anthropic"]["one"]["a"]
    expired = (tokens, metadata, time.time() - 100)
    cache._source_cache["anthropic"]["one"]["a"] = expired

    def forbidden(*args):
        pytest.fail("Snapshot caused durable/database reload")

    monkeypatch.setattr("app.services.source_controls.read_snapshot", forbidden)
    assert await cache.peek_source("anthropic", "one", "a") is None
    assert cache._source_cache["anthropic"]["one"]["a"] == expired
    metadata["durable_source"] = True
    assert (await cache.peek_source("anthropic", "one", "a"))[0] == {"oauth_token": "fixture"}
