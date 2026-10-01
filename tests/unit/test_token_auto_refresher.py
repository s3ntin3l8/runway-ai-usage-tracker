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
async def test_refresh_due_writes_rotated_tokens_back_to_source_bundle(cache, refresher):
    """Collectors read the source-pinned bundle; refreshing only the merged cache left it
    holding a revoked refresh token for providers that rotate them."""
    exp = time.time() + 300
    id_token = _jwt({"exp": exp, "email": "u@example.com"})
    await cache.store(
        "gemini",
        {"oauth_token": "v1", "refresh_token": "rt1", "id_token": id_token},
        account_id="u@example.com",
        source_id="sidecar:host-a:oauth",
    )
    new_id_token = _jwt({"exp": time.time() + 3600, "email": "u@example.com"})
    mock_refresh = AsyncMock(
        return_value={"oauth_token": "v2", "refresh_token": "rt2", "id_token": new_id_token}
    )
    with patch("app.services.token_auto_refresher.refresh_oauth_token", new=mock_refresh):
        assert await refresher.refresh_due() == 1

    (bundle,) = await cache.get_source_candidates("gemini", "u@example.com")
    assert bundle["tokens"]["oauth_token"] == "v2"
    assert bundle["tokens"]["refresh_token"] == "rt2"


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
async def test_refresh_due_strips_dead_oauth_but_keeps_cookie(cache, refresher):
    """An already-expired OAuth token with no refresh_token is stripped during
    the scan (it can never be auto-rolled); a cookie beside it is kept."""
    expired = _jwt({"exp": time.time() - 60, "sub": "x"})
    await cache.store(
        "chatgpt",
        {"oauth_token": expired, "cookie_session": "still-good"},
        account_id="dead",
    )

    with patch(
        "app.services.token_auto_refresher.refresh_oauth_token",
        new=AsyncMock(),
    ):
        await refresher.refresh_due()

    assert await cache.get("chatgpt", "dead") == {"cookie_session": "still-good"}


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


@pytest.mark.asyncio
async def test_refresh_due_refreshes_xai_token_with_xai_refresh(cache, refresher):
    """xAI token with xai_refresh expires in 5 min, threshold 10 min — must auto-refresh."""
    exp = time.time() + 300
    access = _jwt({"exp": exp, "sub": "xai-user-123"})
    await cache.store(
        "xai",
        {"xai_access": access, "xai_refresh": "rt-xai-1"},
        account_id="xai-account",
    )

    new_access = _jwt({"exp": time.time() + 21600, "sub": "xai-user-123"})
    mock_refresh = AsyncMock(
        return_value={
            "xai_access": new_access,
            "xai_refresh": "rt-xai-2",
        }
    )
    with patch("app.services.token_auto_refresher.refresh_oauth_token", new=mock_refresh):
        count = await refresher.refresh_due()

    assert count == 1
    mock_refresh.assert_awaited_once()
    stored = await cache.get("xai", "xai-account")
    assert stored["xai_access"] == new_access
    assert stored["xai_refresh"] == "rt-xai-2"


@pytest.mark.asyncio
async def test_refresh_due_skips_blank_refresh_token(cache, refresher):
    """A blank refresh_token placeholder can't be rolled — don't call the provider."""
    exp = time.time() + 300
    id_token = _jwt({"exp": exp, "email": "u@example.com"})
    await cache.store("gemini", {"oauth_token": "v1", "refresh_token": "", "id_token": id_token})

    mock_refresh = AsyncMock()
    with patch("app.services.token_auto_refresher.refresh_oauth_token", new=mock_refresh):
        count = await refresher.refresh_due()

    assert count == 0
    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_expired_keeps_xai_refresh_bundle(cache):
    """An expired xAI bundle that holds ``xai_refresh`` is still rollable: its OAuth
    fields survive the purge even when an independent credential sits beside them."""
    await cache.store(
        "xai",
        {
            "xai_access": _jwt({"exp": time.time() - 60}),
            "xai_refresh": "rt",
            "api_key": "k",
        },
        account_id="alice@example.com",
    )

    removed = await cache.purge_expired_unrefreshable()

    assert removed == 0
    tokens = await cache.get("xai", "alice@example.com")
    assert tokens is not None and tokens["xai_refresh"] == "rt"
