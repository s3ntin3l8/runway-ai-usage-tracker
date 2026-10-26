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


@pytest.mark.asyncio
async def test_snapshot_nested_metadata_cannot_mutate_live_cache():
    cache = TokenCache()
    await cache.store(
        "anthropic",
        {"oauth_token": "fixture"},
        account_id="one",
        source_id="a",
        source_metadata={"cards": [{"remaining": "10"}]},
    )
    _, metadata = await cache.peek_source("anthropic", "one", "a")
    metadata["cards"][0]["remaining"] = "changed"
    assert (await cache.peek_source("anthropic", "one", "a"))[1]["cards"] == [{"remaining": "10"}]


@pytest.mark.asyncio
async def test_instance_pin_changes_when_the_same_credential_is_replaced():
    cache = TokenCache()
    await cache.store(
        "anthropic", {"cookie_sessionKey": "fixture-secret"}, account_id="one", source_id="a"
    )
    first = await cache.peek_source_instance("anthropic", "one", "a")
    repeated = await cache.peek_source_instance("anthropic", "one", "a")
    assert first == repeated
    assert "fixture-secret" not in repr(first[2])
    await cache.remove_source("anthropic", "one", "a")
    await cache.store(
        "anthropic", {"cookie_sessionKey": "fixture-secret"}, account_id="one", source_id="a"
    )
    replaced = await cache.peek_source_instance("anthropic", "one", "a")
    assert first[:2] == replaced[:2] and first[2] != replaced[2]
    assert await cache.peek_source_instance("anthropic", "other", "a") is None


@pytest.mark.asyncio
async def test_instance_pin_still_protects_live_values_from_mutation():
    cache = TokenCache()
    await cache.store(
        "anthropic",
        {"cookie_sessionKey": "fixture"},
        account_id="one",
        source_id="a",
        source_metadata={"nested": [{"value": "original"}]},
    )
    first = await cache.peek_source_instance("anthropic", "one", "a")
    first[0]["cookie_sessionKey"] = "changed"
    first[1]["nested"][0]["value"] = "changed"
    actual = await cache.peek_source_instance("anthropic", "one", "a")
    assert actual[0]["cookie_sessionKey"] == "fixture"
    assert actual[1]["nested"][0]["value"] == "original"
    assert first[2] == actual[2]
