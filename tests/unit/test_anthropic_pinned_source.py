"""A collector pinned to a source bundle reads only that bundle, and the server has no
Claude credentials-file mode that could be mixed in."""

import base64
import json
import time

import pytest

from app.services.collectors.anthropic import AnthropicCollector
from app.services.token_cache import TokenCache

ACCOUNT = "bob@example.com"
SOURCE = "sidecar:host-b:oauth"


def _jwt(payload: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


@pytest.fixture
def cache(monkeypatch) -> TokenCache:
    fresh = TokenCache()
    for module in (
        "app.services.collectors.anthropic.token_cache",
        "app.services.collectors.anthropic_oauth.token_cache",
        "app.services.collectors.oauth_base.token_cache",
    ):
        monkeypatch.setattr(module, fresh)
    return fresh


def _collector() -> AnthropicCollector:
    return AnthropicCollector(account_id=ACCOUNT)


@pytest.mark.asyncio
async def test_collector_reads_no_credentials_file(cache, tmp_path, monkeypatch):
    """The collector has no file mode: no ``_get_credentials``/``_persist_credentials``."""
    collector = _collector()
    assert not hasattr(collector, "_get_credentials")
    assert not hasattr(collector, "_persist_credentials")
    assert not hasattr(collector, "_credentials_path")


@pytest.mark.asyncio
async def test_pinned_expiry_comes_from_the_bundle(cache):
    # Bob's bundle expired a minute ago.
    expired = _jwt({"exp": time.time() - 60})
    await cache.store(
        "anthropic",
        {"oauth_token": expired, "refresh_token": "bob-refresh"},
        account_id=ACCOUNT,
        source_id=SOURCE,
    )
    collector = _collector()

    assert await collector._is_token_expired() is False  # unpinned → tried as-is
    async with cache.using_source("anthropic", ACCOUNT, SOURCE):
        assert await collector._is_token_expired() is True
        assert await collector._is_token_expiring_soon() is True


@pytest.mark.asyncio
async def test_refresh_result_is_written_into_the_pinned_bundle(cache):
    await cache.store(
        "anthropic",
        {"oauth_token": "bob-old", "refresh_token": "bob-rt1"},
        account_id=ACCOUNT,
        source_id=SOURCE,
    )
    collector = _collector()

    async with cache.using_source("anthropic", ACCOUNT, SOURCE):
        await collector._store_sidecar_token("anthropic", "bob-new", "bob-rt2")

    (bundle,) = await cache.get_source_candidates("anthropic", ACCOUNT)
    assert bundle["tokens"]["oauth_token"] == "bob-new"
    assert bundle["tokens"]["refresh_token"] == "bob-rt2"
