"""A collector pinned to a source bundle must not mix in the server host's own
Claude credentials file (it belongs to whichever account is logged in there)."""

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


@pytest.fixture
def server_file(tmp_path):
    """The server host's own login: a *different* account than the sidecar's."""
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "server-access",
                    "refreshToken": "server-refresh",
                    "expiresAt": int((time.time() + 3600) * 1000),
                },
                "oauthAccount": {"emailAddress": "alice@example.com"},
            }
        )
    )
    return path


def _collector(server_file) -> AnthropicCollector:
    collector = AnthropicCollector(account_id=ACCOUNT)
    collector._credentials_path = str(server_file)
    return collector


@pytest.mark.asyncio
async def test_pinned_collector_ignores_server_credentials_file(cache, server_file):
    await cache.store(
        "anthropic",
        {"oauth_token": "bob-access", "refresh_token": "bob-refresh"},
        account_id=ACCOUNT,
        source_id=SOURCE,
    )
    collector = _collector(server_file)

    # Unpinned (a pure server deployment): the host's file is used, as before.
    assert (await collector._get_credentials())["oauthAccount"]["emailAddress"] == (
        "alice@example.com"
    )

    async with cache.using_source("anthropic", ACCOUNT, SOURCE):
        assert await collector._get_credentials() is None


@pytest.mark.asyncio
async def test_pinned_collector_never_overwrites_server_file(cache, server_file):
    await cache.store(
        "anthropic", {"oauth_token": "bob-access"}, account_id=ACCOUNT, source_id=SOURCE
    )
    collector = _collector(server_file)
    before = server_file.read_text()

    async with cache.using_source("anthropic", ACCOUNT, SOURCE):
        collector._persist_credentials({"claudeAiOauth": {"accessToken": "bob-new"}})
    assert server_file.read_text() == before

    # Unpinned persistence still works.
    collector._persist_credentials({"claudeAiOauth": {"accessToken": "server-new"}})
    assert "server-new" in server_file.read_text()


@pytest.mark.asyncio
async def test_pinned_expiry_comes_from_the_bundle_not_the_server_file(cache, server_file):
    # The server file's token is fresh for an hour; Bob's bundle expired a minute ago.
    expired = _jwt({"exp": time.time() - 60})
    await cache.store(
        "anthropic",
        {"oauth_token": expired, "refresh_token": "bob-refresh"},
        account_id=ACCOUNT,
        source_id=SOURCE,
    )
    collector = _collector(server_file)

    assert await collector._is_token_expired() is False  # unpinned → the server file
    async with cache.using_source("anthropic", ACCOUNT, SOURCE):
        assert await collector._is_token_expired() is True
        assert await collector._is_token_expiring_soon() is True


@pytest.mark.asyncio
async def test_refresh_result_is_written_into_the_pinned_bundle(cache, server_file):
    await cache.store(
        "anthropic",
        {"oauth_token": "bob-old", "refresh_token": "bob-rt1"},
        account_id=ACCOUNT,
        source_id=SOURCE,
    )
    collector = _collector(server_file)

    async with cache.using_source("anthropic", ACCOUNT, SOURCE):
        await collector._store_sidecar_token("anthropic", "bob-new", "bob-rt2")

    (bundle,) = await cache.get_source_candidates("anthropic", ACCOUNT)
    assert bundle["tokens"]["oauth_token"] == "bob-new"
    assert bundle["tokens"]["refresh_token"] == "bob-rt2"
