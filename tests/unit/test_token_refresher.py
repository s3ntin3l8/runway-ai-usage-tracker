"""Unit tests for app/services/token_refresher.py"""

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
        data = (
            call_kwargs.kwargs.get("data") or call_kwargs.args[1]
            if len(call_kwargs.args) > 1
            else call_kwargs.kwargs["data"]
        )
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
        assert "client_id" in sent_data  # Falls back to settings
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

    async def test_persist_to_local_file_writes_gemini_creds(self, tmp_path, monkeypatch):
        """Refreshed tokens get written back to the gcloud-format file."""
        import json

        from app.services import token_refresher

        path = tmp_path / "oauth_creds.json"
        existing = {
            "access_token": "old_at",
            "refresh_token": "old_rt",
            "id_token": "old_idt",
            "scope": "openid profile email",
            "token_type": "Bearer",
            "expiry_date": 0,
        }
        path.write_text(json.dumps(existing))
        monkeypatch.setattr(token_refresher.settings, "GEMINI_OAUTH_PATH", str(path))

        # Build a JWT with exp = 1000s in the future
        import base64
        import time

        def _jwt(payload):
            def b64(d):
                return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

            return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"

        future = int(time.time()) + 3600
        new_idt = _jwt({"exp": future, "email": "u@example.com"})

        token_refresher.persist_to_local_file(
            "gemini",
            {"oauth_token": "new_at", "refresh_token": "new_rt", "id_token": new_idt},
            source="server",
        )

        written = json.loads(path.read_text())
        assert written["access_token"] == "new_at"
        assert written["refresh_token"] == "new_rt"
        assert written["id_token"] == new_idt
        assert written["expiry_date"] == future * 1000
        # Untouched fields preserved
        assert written["scope"] == "openid profile email"

    async def test_persist_to_local_file_handles_non_numeric_exp(self, tmp_path, monkeypatch):
        """A non-numeric JWT exp must not raise — expiry_date is left untouched."""
        import base64
        import json

        from app.services import token_refresher

        path = tmp_path / "oauth_creds.json"
        path.write_text(json.dumps({"access_token": "old_at", "expiry_date": 0}))
        monkeypatch.setattr(token_refresher.settings, "GEMINI_OAUTH_PATH", str(path))

        def _jwt(payload):
            def b64(d):
                return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

            return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"

        bad_idt = _jwt({"exp": "not-a-number", "email": "u@example.com"})

        # Must not raise despite the unparseable exp claim
        token_refresher.persist_to_local_file(
            "gemini",
            {"oauth_token": "new_at", "id_token": bad_idt},
            source="server",
        )

        written = json.loads(path.read_text())
        assert written["access_token"] == "new_at"
        # exp was unparseable → expiry_date was not overwritten
        assert written["expiry_date"] == 0

    async def test_persist_to_local_file_skips_non_server_source(self, tmp_path, monkeypatch):
        """Tokens that didn't come from the local file must not overwrite it."""
        import json

        from app.services import token_refresher

        path = tmp_path / "oauth_creds.json"
        path.write_text(json.dumps({"access_token": "untouched"}))
        monkeypatch.setattr(token_refresher.settings, "GEMINI_OAUTH_PATH", str(path))

        token_refresher.persist_to_local_file(
            "gemini",
            {"oauth_token": "sidecar_token"},
            source=None,
        )

        assert json.loads(path.read_text())["access_token"] == "untouched"

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
