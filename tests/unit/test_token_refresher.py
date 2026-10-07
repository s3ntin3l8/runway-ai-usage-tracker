"""Unit tests for app/services/token_refresher.py"""

import json
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.token_refresher import (
    _PROVIDER_CLIENT_IDS,
    ANTHROPIC_OAUTH_SCOPES,
    ANTHROPIC_REFRESH_USER_AGENT,
    refresh_oauth_token,
)


def _make_mock_response(status_code: int, body: dict) -> MagicMock:
    """Build a fake httpx.Response-like object."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = json.dumps(body)
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
    async def test_sends_claude_codes_own_request_shape(self):
        """JSON body incl. ``scope`` and no header but Content-Type (+ a neutral User-Agent).

        The form body without ``scope`` plus ``claude-code/2.1.69`` / ``anthropic-beta`` that
        this module used to send was answered with HTTP 429 every time (issue #577)."""
        resp = _make_mock_response(200, {"access_token": "new_access", "token_type": "Bearer"})
        ctx = _make_async_client(resp)

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token(
                "anthropic", {"refresh_token": "old_refresh", "client_id": "my_client_id"}
            )

        post = ctx.__aenter__.return_value.post
        assert post.call_args.args[0] == "https://platform.claude.com/v1/oauth/token"
        kwargs = post.call_args.kwargs
        assert "data" not in kwargs  # not form-encoded
        assert kwargs["json"] == {
            "grant_type": "refresh_token",
            "refresh_token": "old_refresh",
            "client_id": "my_client_id",
            "scope": " ".join(ANTHROPIC_OAUTH_SCOPES),
        }
        assert kwargs["headers"] == {
            "Content-Type": "application/json",
            "User-Agent": ANTHROPIC_REFRESH_USER_AGENT,
        }
        assert result["oauth_token"] == "new_access"

    async def test_falls_back_to_the_configured_client_id(self):
        ctx = _make_async_client(_make_mock_response(200, {"access_token": "new_access"}))

        with patch("httpx.AsyncClient", return_value=ctx):
            await refresh_oauth_token("anthropic", {"refresh_token": "old_refresh"})

        sent = ctx.__aenter__.return_value.post.call_args.kwargs["json"]
        assert sent["client_id"] == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

    async def test_requests_the_stored_scope_when_the_bundle_has_one(self):
        ctx = _make_async_client(_make_mock_response(200, {"access_token": "t"}))

        with patch("httpx.AsyncClient", return_value=ctx):
            await refresh_oauth_token(
                "anthropic", {"refresh_token": "rt", "scope": "user:profile user:inference"}
            )

        sent = ctx.__aenter__.return_value.post.call_args.kwargs["json"]
        assert sent["scope"] == "user:profile user:inference"

    async def test_sends_none_of_the_headers_that_get_throttled(self):
        ctx = _make_async_client(_make_mock_response(200, {"access_token": "tok"}))

        with patch("httpx.AsyncClient", return_value=ctx):
            await refresh_oauth_token("anthropic", {"refresh_token": "rt"})

        headers = ctx.__aenter__.return_value.post.call_args.kwargs["headers"]
        assert "anthropic-beta" not in headers
        assert "claude-code" not in headers["User-Agent"]
        assert "Accept" not in headers

    @pytest.mark.parametrize("provider", ["gemini", "chatgpt", "xai"])
    async def test_other_providers_still_send_a_form_body(self, provider):
        ctx = _make_async_client(_make_mock_response(200, {"access_token": "tok"}))

        with patch("httpx.AsyncClient", return_value=ctx):
            await refresh_oauth_token(provider, {"refresh_token": "rt"})

        kwargs = ctx.__aenter__.return_value.post.call_args.kwargs
        assert kwargs["data"]["grant_type"] == "refresh_token"
        assert "json" not in kwargs
        assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"

    def test_the_scope_list_matches_the_sidecar_renewers(self):
        """The sidecar cannot import ``app``, so the two lists are kept equal by a test."""
        from scripts.sidecar_pkg import anthropic_renewer

        assert tuple(ANTHROPIC_OAUTH_SCOPES) == tuple(anthropic_renewer.DEFAULT_SCOPES)
        assert _PROVIDER_CLIENT_IDS["anthropic"] == anthropic_renewer.CLIENT_ID

    async def test_sets_oauth_token_from_access_token(self):
        body = {"access_token": "brand_new_token"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("anthropic", {"refresh_token": "rt"})

        assert result["oauth_token"] == "brand_new_token"
        # Original token keys still present
        assert result["refresh_token"] == "rt"

    async def test_captures_expiry_date_from_expires_in(self):
        # The new access token's expiry must be recorded (ms epoch) so the cache
        # freshness guard can tell a refreshed token from a staler sidecar push.
        body = {"access_token": "tok", "expires_in": 3600}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("anthropic", {"refresh_token": "rt"})

        assert "expiry_date" in result
        # ~1h in the future, expressed in ms.
        assert int(result["expiry_date"]) > int(time.time() * 1000) + 3_500_000


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

    async def test_works_without_optional_client_id_and_secret(self, monkeypatch):
        from app.services import token_refresher

        monkeypatch.setattr(token_refresher.settings, "GEMINI_OAUTH_CLIENT_ID", "fallback_id")
        monkeypatch.setattr(
            token_refresher.settings, "GEMINI_OAUTH_CLIENT_SECRET", "fallback_secret"
        )

        body = {"access_token": "gemini_access"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        tokens = {"refresh_token": "g_refresh"}

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("gemini", tokens)

        sent_data = ctx.__aenter__.return_value.post.call_args.kwargs["data"]
        assert sent_data["client_id"] == "fallback_id"
        assert sent_data["client_secret"] == "fallback_secret"
        assert result["oauth_token"] == "gemini_access"

    async def test_falls_back_to_id_token_aud_for_client_id(self, monkeypatch):
        """Gemini CLI tokens carry the client_id only as the JWT aud claim."""
        import base64
        import json

        from app.services import token_refresher

        monkeypatch.setattr(token_refresher.settings, "GEMINI_OAUTH_CLIENT_ID", "")

        def _jwt(payload: dict) -> str:
            def b64(d):
                return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

            return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"

        id_token = _jwt({"aud": "cli-app.apps.googleusercontent.com"})

        body = {"access_token": "gemini_access"}
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        tokens = {"refresh_token": "g_refresh", "id_token": id_token}

        with patch("httpx.AsyncClient", return_value=ctx):
            await refresh_oauth_token("gemini", tokens)

        sent_data = ctx.__aenter__.return_value.post.call_args.kwargs["data"]
        assert sent_data["client_id"] == "cli-app.apps.googleusercontent.com"


class TestRefreshOAuthTokenHTTPErrors:
    async def test_http_4xx_raises_http_status_error(self):
        resp = _make_mock_response(401, {"error": "invalid_token"})
        ctx = _make_async_client(resp)

        with patch("httpx.AsyncClient", return_value=ctx):
            with pytest.raises(httpx.HTTPStatusError):
                await refresh_oauth_token("anthropic", {"refresh_token": "rt"})

    async def test_http_error_logs_status_and_oauth_error_code(self, caplog):
        """`invalid_grant` vs `invalid_client` is the whole diagnosis — log it.

        A dead refresh lineage used to surface as nothing more than
        `HTTPStatusError: HTTP 400`, which cannot distinguish a rotated grant
        from a malformed request (issue #474).
        """
        resp = _make_mock_response(400, {"error": "invalid_grant"})
        ctx = _make_async_client(resp)

        with (
            patch("httpx.AsyncClient", return_value=ctx),
            caplog.at_level(logging.WARNING, logger="app.services.token_refresher"),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await refresh_oauth_token("xai", {"xai_refresh": "rt"})

        message = "\n".join(record.getMessage() for record in caplog.records)
        assert "provider=xai" in message
        assert "status=400" in message
        assert "invalid_grant" in message

    async def test_http_error_body_is_redacted_and_truncated(self, caplog):
        """Token-shaped and PII content in the body must not reach the log."""
        body = {
            "error": "invalid_grant",
            "access_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.sig",  # pragma: allowlist secret
            "detail": "alice@example.com is not registered",
            "verbose": "x" * 5000,
        }
        resp = _make_mock_response(400, body)
        ctx = _make_async_client(resp)

        with (
            patch("httpx.AsyncClient", return_value=ctx),
            caplog.at_level(logging.WARNING, logger="app.services.token_refresher"),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await refresh_oauth_token("xai", {"xai_refresh": "rt"})

        message = "\n".join(record.getMessage() for record in caplog.records)
        assert "eyJhbGciOiJIUzI1NiJ9" not in message
        assert "alice@example.com" not in message
        assert '"access_token": "[REDACTED]"' in message  # whole value, dict path
        assert "[REDACTED" in message
        assert "invalid_grant" in message
        assert len(message) < 1000  # body capped well below its raw 5 kB size

    async def test_non_json_error_body_falls_back_to_string_redaction(self, caplog):
        """HTML/plain error pages still get redacted on the string path."""
        resp = _make_mock_response(400, {"error": "invalid_client"})
        resp.text = "<html>client rejected for alice@example.com</html>"
        ctx = _make_async_client(resp)

        with (
            patch("httpx.AsyncClient", return_value=ctx),
            caplog.at_level(logging.WARNING, logger="app.services.token_refresher"),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await refresh_oauth_token("xai", {"xai_refresh": "rt"})

        message = "\n".join(record.getMessage() for record in caplog.records)
        assert "<html>" in message
        assert "alice@example.com" not in message
        assert "[REDACTED_EMAIL]" in message


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


class TestRefreshOAuthTokenXai:
    async def test_xai_refresh_sends_correct_payload_and_updates_keys(self):
        body = {
            "access_token": "fresh_xai_access",
            "token_type": "bearer",
            "expires_in": 21600,
            "refresh_token": "fresh_xai_refresh",
            "scope": "openid grok-cli:access",
        }
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)

        tokens = {
            "xai_access": "stale_xai_access",
            "xai_refresh": "current_xai_refresh",
        }

        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("xai", tokens)

        post_mock = ctx.__aenter__.return_value.post
        call_args, call_kwargs = post_mock.call_args
        assert call_args[0] == "https://auth.x.ai/oauth2/token"
        assert call_kwargs["data"]["grant_type"] == "refresh_token"
        assert call_kwargs["data"]["refresh_token"] == "current_xai_refresh"
        assert call_kwargs["data"]["client_id"] == "b1a00492-073a-47ea-816f-4c329264a828"
        assert call_kwargs["headers"]["User-Agent"] == "opencode/1.0"

        assert result["xai_access"] == "fresh_xai_access"
        assert result["xai_refresh"] == "fresh_xai_refresh"
        assert "oauth_token" not in result
        assert "refresh_token" not in result
        assert "expiry_date" in result
        exp_ms = int(result["expiry_date"])
        assert exp_ms > int(time.time() * 1000)

    async def test_non_xai_refresh_does_not_update_xai_keys(self):
        body = {
            "access_token": "new_anthropic_token",
            "refresh_token": "new_anthropic_refresh",
        }
        resp = _make_mock_response(200, body)
        ctx = _make_async_client(resp)
        tokens = {
            "oauth_token": "old_token",
            "refresh_token": "old_refresh",
            "xai_access": "should_not_change",
            "xai_refresh": "should_not_change",
        }
        with patch("httpx.AsyncClient", return_value=ctx):
            result = await refresh_oauth_token("anthropic", tokens)

        assert result["oauth_token"] == "new_anthropic_token"
        assert result["refresh_token"] == "new_anthropic_refresh"
        assert result["xai_access"] == "should_not_change"
        assert result["xai_refresh"] == "should_not_change"
