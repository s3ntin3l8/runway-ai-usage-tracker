"""Unit tests for app.services.token_auto_refresher."""

import base64
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.services.token_auto_refresher import TokenAutoRefresher
from app.services.token_cache import TokenCache


def _jwt(payload: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


@pytest.fixture
def cache(monkeypatch):
    fresh = TokenCache()
    monkeypatch.setattr("app.services.token_auto_refresher.token_cache", fresh)
    return fresh


@pytest.fixture
def refresher():
    return TokenAutoRefresher(interval_seconds=300, threshold_seconds=600)


@pytest.mark.asyncio
async def test_refresh_due_skips_token_far_from_expiry(cache, refresher):
    """Token with 1 hour left and threshold of 10 min — should not refresh."""
    exp = time.time() + 3600
    id_token = _jwt({"exp": exp, "email": "u@example.com"})
    await cache.store(
        "gemini",
        {"oauth_token": "v1", "refresh_token": "rt", "id_token": id_token},
    )

    with patch(
        "app.services.token_auto_refresher.refresh_oauth_token",
        new=AsyncMock(),
    ) as mock_refresh:
        count = await refresher.refresh_due()

    assert count == 0
    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_due_refreshes_token_inside_threshold(cache, refresher):
    """Token expires in 5 min, threshold is 10 min — must refresh."""
    exp = time.time() + 300
    id_token = _jwt({"exp": exp, "email": "u@example.com"})
    await cache.store(
        "gemini",
        {"oauth_token": "v1", "refresh_token": "rt", "id_token": id_token},
    )

    new_id_token = _jwt({"exp": time.time() + 3600, "email": "u@example.com"})
    mock_refresh = AsyncMock(
        return_value={
            "oauth_token": "v2",
            "refresh_token": "rt",
            "id_token": new_id_token,
        }
    )
    with patch("app.services.token_auto_refresher.refresh_oauth_token", new=mock_refresh):
        count = await refresher.refresh_due()

    assert count == 1
    mock_refresh.assert_awaited_once()
    # Cache now holds the rotated token
    tokens = await cache.get("gemini", "u@example.com")
    assert tokens is not None
    assert tokens["oauth_token"] == "v2"


@pytest.mark.asyncio
async def test_refresh_due_skips_when_no_refresh_token(cache, refresher):
    """Tokens without a refresh_token can't be refreshed — skip silently."""
    exp = time.time() + 60
    id_token = _jwt({"exp": exp, "email": "u@example.com"})
    await cache.store("gemini", {"oauth_token": "v1", "id_token": id_token})

    with patch(
        "app.services.token_auto_refresher.refresh_oauth_token",
        new=AsyncMock(),
    ) as mock_refresh:
        count = await refresher.refresh_due()

    assert count == 0
    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_due_skips_opaque_tokens(cache, refresher):
    """Without a JWT exp claim we have no signal to refresh on — skip."""
    await cache.store(
        "gemini",
        {"oauth_token": "opaque", "refresh_token": "rt"},
    )

    with patch(
        "app.services.token_auto_refresher.refresh_oauth_token",
        new=AsyncMock(),
    ) as mock_refresh:
        count = await refresher.refresh_due()

    assert count == 0
    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_due_swallows_individual_failures(cache, refresher):
    """One failing token must not block others in the same scan."""
    exp = time.time() + 60
    id_token_a = _jwt({"exp": exp, "email": "a@example.com"})
    id_token_b = _jwt({"exp": exp, "email": "b@example.com"})
    await cache.store(
        "gemini",
        {"oauth_token": "va", "refresh_token": "rt", "id_token": id_token_a},
    )
    await cache.store(
        "gemini",
        {"oauth_token": "vb", "refresh_token": "rt", "id_token": id_token_b},
    )

    new_token_b = _jwt({"exp": time.time() + 3600, "email": "b@example.com"})

    async def fake_refresh(provider, tokens):
        if tokens.get("oauth_token") == "va":
            raise RuntimeError("upstream 500")
        return {"oauth_token": "vb2", "refresh_token": "rt", "id_token": new_token_b}

    with patch(
        "app.services.token_auto_refresher.refresh_oauth_token",
        side_effect=fake_refresh,
    ):
        count = await refresher.refresh_due()

    assert count == 1
    tokens_b = await cache.get("gemini", "b@example.com")
    assert tokens_b["oauth_token"] == "vb2"


@pytest.mark.asyncio
async def test_refresh_due_ignores_providers_without_refresh_endpoint(cache, refresher):
    """If the provider isn't in _REFRESH_ENDPOINTS we have no way to refresh."""
    exp = time.time() + 60
    id_token = _jwt({"exp": exp, "email": "u@example.com"})
    await cache.store(
        "github",  # not in _REFRESH_ENDPOINTS
        {"oauth_token": "v1", "refresh_token": "rt", "id_token": id_token},
    )

    with patch(
        "app.services.token_auto_refresher.refresh_oauth_token",
        new=AsyncMock(),
    ) as mock_refresh:
        count = await refresher.refresh_due()

    assert count == 0
    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_due_purges_dead_unrefreshable_tokens(cache, refresher):
    """An already-expired token with no refresh_token is evicted during the scan.

    These can never be auto-rolled, so they'd otherwise linger as a stale
    "expired" entry in Token Health (and trip the dashboard banner).
    """
    expired = _jwt({"exp": time.time() - 60, "sub": "x"})
    await cache.store("chatgpt", {"oauth_token": expired}, account_id="dead")

    with patch(
        "app.services.token_auto_refresher.refresh_oauth_token",
        new=AsyncMock(),
    ):
        await refresher.refresh_due()

    assert await cache.get("chatgpt", "dead") is None


@pytest.mark.asyncio
async def test_start_stop_lifecycle(refresher):
    refresher.start()
    assert refresher._task is not None
    assert refresher._running is True

    # Idempotent
    refresher.start()
    await refresher.stop()
    assert refresher._task is None
    assert refresher._running is False
