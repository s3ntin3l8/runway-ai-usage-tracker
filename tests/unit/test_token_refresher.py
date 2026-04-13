"""Unit tests for app/services/token_refresher.py"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.token_refresher import refresh_oauth_token


def _make_mock_response(status_code: int, body: dict) -> MagicMock:
    """Build a fake httpx.Response-like object."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = body
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _make_async_client(response: MagicMock) -> MagicMock:
    """Create a mock async context-manager client whose .post() returns *response*."""
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    # Support `async with httpx.AsyncClient(...) as client:`
    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=client)
    async_ctx.__aexit__ = AsyncMock(return_value=False)
    return async_ctx


class TestRefreshOAuthTokenUnknownProvider:
    async def test_raises_value_error_for_unknown_provider(self):
        with pytest.raises(ValueError, match="unknown_provider"):
            await refresh_oauth_token("unknown_provider", {"refresh_token": "rt"})


class TestRefreshOAuthTokenAnthropic:
    async def test_sends_correct_payload_with_client_id(self):
        body = {"access_token": "new_access", "token_type": "Bearer"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        tokens = {
            "refresh_token": "old_refresh",
            "client_id": "my_client_id",
        }

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("anthropic", tokens)

        call_kwargs = ctx.__aenter__.return_value.post.call_args
        assert call_kwargs is not None
        data = call_kwargs.kwargs.get("data") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs["data"]
        # Access via keyword arg 'data'
        sent_data = call_kwargs.kwargs["data"]
        assert sent_data["grant_type"] == "refresh_token"
        assert sent_data["refresh_token"] == "old_refresh"
        assert sent_data["client_id"] == "my_client_id"

        sent_headers = call_kwargs.kwargs["headers"]
        assert sent_headers["User-Agent"] == "claude-code/2.1.69"
        assert sent_headers["anthropic-beta"] == "oauth-2025-04-20"

        assert result["oauth_token"] == "new_access"

    async def test_sends_correct_payload_without_client_id(self):
        body = {"access_token": "new_access"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        tokens = {"refresh_token": "old_refresh"}

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("anthropic", tokens)

        sent_data = ctx.__aenter__.return_value.post.call_args.kwargs["data"]
        assert "client_id" not in sent_data
        assert sent_data["grant_type"] == "refresh_token"
        assert result["oauth_token"] == "new_access"

    async def test_has_anthropic_specific_headers(self):
        body = {"access_token": "tok"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        with patch("httpx.AsyncClient", return_value=ctx):
            await refresh_oauth_token("anthropic", {"refresh_token": "rt"})

        headers = ctx.__aenter__.return_value.post.call_args.kwargs["headers"]
        assert "User-Agent" in headers
        assert "anthropic-beta" in headers

    async def test_sets_oauth_token_from_access_token(self):
        body = {"access_token": "brand_new_token"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("anthropic", {"refresh_token": "rt"})

        assert result["oauth_token"] == "brand_new_token"
        # Original token keys still present
        assert result["refresh_token"] == "rt"


class TestRefreshOAuthTokenGemini:
    async def test_sends_correct_payload_with_client_id_and_secret(self):
        body = {"access_token": "gemini_access"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        tokens = {
            "refresh_token": "g_refresh",
            "client_id": "g_client",
            "client_secret": "g_secret",
        }

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("gemini", tokens)

        sent_data = ctx.__aenter__.return_value.post.call_args.kwargs["data"]
        assert sent_data["grant_type"] == "refresh_token"
        assert sent_data["refresh_token"] == "g_refresh"
        assert sent_data["client_id"] == "g_client"
        assert sent_data["client_secret"] == "g_secret"

        # No Anthropic-specific headers
        headers = ctx.__aenter__.return_value.post.call_args.kwargs["headers"]
        assert "User-Agent" not in headers
        assert "anthropic-beta" not in headers

        assert result["oauth_token"] == "gemini_access"

    async def test_works_without_optional_client_id_and_secret(self):
        body = {"access_token": "gemini_access"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        tokens = {"refresh_token": "g_refresh"}

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("gemini", tokens)

        sent_data = ctx.__aenter__.return_value.post.call_args.kwargs["data"]
        assert "client_id" not in sent_data
        assert "client_secret" not in sent_data
        assert result["oauth_token"] == "gemini_access"


class TestRefreshOAuthTokenHTTPErrors:
    async def test_http_4xx_raises_http_status_error(self):
        resp = _make_mock_response(401, {"error": "invalid_token"})
        ctx = _make_async_client(resp)

        with patch("httpx.AsyncClient", return_value=ctx):
            with pytest.raises(httpx.HTTPStatusError):
                await refresh_oauth_token("anthropic", {"refresh_token": "rt"})


class TestRefreshOAuthTokenTokenRotation:
    async def test_response_with_refresh_token_rotates_it(self):
        body = {"access_token": "new_access", "refresh_token": "new_refresh"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        tokens = {"refresh_token": "old_refresh"}

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("anthropic", tokens)

        assert result["refresh_token"] == "new_refresh"
        assert result["oauth_token"] == "new_access"

    async def test_response_without_refresh_token_keeps_original(self):
        body = {"access_token": "new_access"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        tokens = {"refresh_token": "original_refresh"}

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("anthropic", tokens)

        assert result["refresh_token"] == "original_refresh"
        assert result["oauth_token"] == "new_access"
