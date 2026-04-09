import pytest
import os
import json
import httpx
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
from datetime import datetime, timezone, timedelta

from app.services.collectors.chatgpt import ChatGPTCollector
from app.core.config import settings


@pytest.fixture
def chatgpt_account_response():
    """Mock response for ChatGPT accounts endpoint."""
    return {
        "accounts": {
            "user-123": {
                "account_user_role": "owner",
                "account_status": "active",
                "entitlements": [{"slug": "plus"}],
            }
        }
    }


@pytest.fixture
def chatgpt_usage_response():
    """Mock response for ChatGPT wham/usage endpoint."""
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 25.5,
                "reset_at": int(
                    (datetime.now(timezone.utc) + timedelta(hours=3)).timestamp()
                ),
            }
        },
    }


class TestChatGPTCollectorDetailed:
    """Detailed unit tests for ChatGPTCollector's complex logic."""

    @pytest.mark.asyncio
    async def test_auth_priority_env(self, mock_http_client):
        """Priority 1: Environment variable should be used first."""
        collector = ChatGPTCollector()
        with patch.dict("os.environ", {"CHATGPT_OAUTH_TOKEN": "env_token"}):
            auth = await collector._get_auth_data(mock_http_client)
            assert auth["token"] == "env_token"
            assert auth["source"] == "credential_provider"

    @pytest.mark.asyncio
    async def test_auth_priority_file(self, mock_http_client):
        """Priority 2: ~/.codex/auth.json should be used if no env var."""
        collector = ChatGPTCollector()
        mock_auth = json.dumps({"tokens": {"access_token": "file_token"}})

        with patch.dict("os.environ", {}, clear=True):
            with patch("os.path.exists", return_value=True):
                with patch("builtins.open", mock_open(read_data=mock_auth)):
                    auth = await collector._get_auth_data(mock_http_client)
                    assert auth["token"] == "file_token"
                    assert auth["source"] == "credential_provider"

    @pytest.mark.asyncio
    async def test_auth_priority_cookies_and_refresh(self, mock_http_client):
        """Priority 3: Browser cookies should trigger a token refresh."""
        collector = ChatGPTCollector()
        session_token = "fake_session_token"
        refreshed_token = "refreshed_bearer_token"

        # Mock cookie extraction
        with patch.dict("os.environ", {}, clear=True):
            with patch("os.path.exists", return_value=False):
                with patch(
                    "app.services.collectors.chatgpt.get_chatgpt_session_token",
                    return_value=session_token,
                ):
                    # Mock the refresh API call
                    mock_resp = MagicMock(spec=httpx.Response)
                    mock_resp.status_code = 200
                    mock_resp.json.return_value = {"accessToken": refreshed_token}
                    mock_http_client.get.return_value = mock_resp

                    auth = await collector._get_auth_data(mock_http_client)

                    assert auth["token"] == refreshed_token
                    assert auth["source"] == "cookies"

                    # Verify refresh URL was called with cookie
                    call_args = mock_http_client.get.call_args
                    assert "api/auth/session" in str(call_args)
                    assert (
                        f"__Secure-next-auth.session-token={session_token}"
                        in call_args.kwargs["headers"]["Cookie"]
                    )

    @pytest.mark.asyncio
    async def test_token_refresh_caching(self, mock_http_client):
        """Refreshed token should be cached in memory for 1 hour."""
        collector = ChatGPTCollector()
        session_token = "fake_session_token"

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"accessToken": "t1"}
        mock_http_client.get.return_value = mock_resp

        # Patch EVERYTHING to ensure we hit the cookie path
        with patch.dict("os.environ", {}, clear=True):
            with patch("os.path.exists", return_value=False):
                with patch(
                    "app.services.collectors.chatgpt.get_chatgpt_session_token",
                    return_value=session_token,
                ):
                    # First refresh
                    auth1 = await collector._get_auth_data(mock_http_client)
                    assert auth1["token"] == "t1"
                    assert mock_http_client.get.call_count == 1

                    # Second call immediately - should use in-memory cache
                    auth2 = await collector._get_auth_data(mock_http_client)
                    assert auth2["token"] == "t1"
                    assert auth2["source"] == "cookies_cached"
                    assert mock_http_client.get.call_count == 1  # No new API call

    @pytest.mark.asyncio
    async def test_tier_detection_plus(
        self, mock_http_client, chatgpt_account_response, chatgpt_usage_response
    ):
        """Verify 'PLUS' tier detection from entitlements."""
        collector = ChatGPTCollector()

        acc_resp = MagicMock(spec=httpx.Response)
        acc_resp.status_code = 200
        acc_resp.json.return_value = chatgpt_account_response  # contains "plus"

        usage_resp = MagicMock(spec=httpx.Response)
        usage_resp.status_code = 200
        usage_resp.json.return_value = chatgpt_usage_response

        mock_http_client.get.side_effect = [acc_resp, usage_resp]

        # Isolation
        with patch.dict("os.environ", {"CHATGPT_OAUTH_TOKEN": "token"}):
            results = await collector.collect(mock_http_client)

            assert results[0]["service"] == "ChatGPT Account"
            assert results[0]["remaining"] == "PLUS"
            assert "Plus" in results[0]["detail"]

    @pytest.mark.asyncio
    async def test_tier_detection_free(self, mock_http_client, chatgpt_usage_response):
        """Verify 'FREE' tier detection when no 'plus' entitlement exists."""
        collector = ChatGPTCollector()

        acc_resp = MagicMock(spec=httpx.Response)
        acc_resp.status_code = 200
        acc_resp.json.return_value = {"accounts": {"u": {"entitlements": []}}}

        usage_resp = MagicMock(spec=httpx.Response)
        usage_resp.status_code = 200
        usage_resp.json.return_value = chatgpt_usage_response

        mock_http_client.get.side_effect = [acc_resp, usage_resp]

        with patch.dict("os.environ", {"CHATGPT_OAUTH_TOKEN": "token"}):
            results = await collector.collect(mock_http_client)
            assert results[0]["remaining"] == "FREE"

    @pytest.mark.asyncio
    async def test_collect_api_failure_fallback(self, mock_http_client):
        """Verify fallback to local logs when API returns 429/500."""
        collector = ChatGPTCollector()

        # Mock API failure
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 429
        mock_http_client.get.return_value = mock_resp

        # Mock local logs existence
        log_content = json.dumps({"used_percent": 88.0, "resets_at": 1744876800})

        with patch.dict("os.environ", {"CHATGPT_OAUTH_TOKEN": "token"}):
            with patch(
                "app.services.collectors.chatgpt.glob.glob",
                return_value=["/fake/path/session.jsonl"],
            ):
                with patch("os.path.getmtime", return_value=12345):
                    with patch("builtins.open", mock_open(read_data=log_content)):
                        results = await collector.collect(mock_http_client)

                        assert len(results) == 1
                        assert results[0]["service"] == "ChatGPT Codex"
                        assert "12.0%" in results[0]["remaining"]
                        assert results[0]["data_source"] == "cache"

    @pytest.mark.asyncio
    async def test_user_agent_on_auth_refresh(self, mock_http_client):
        """Check that a Chrome-like User-Agent is used for auth session refresh."""
        collector = ChatGPTCollector()

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"accessToken": "new_token"}
        mock_http_client.get.return_value = mock_resp

        await collector._refresh_access_token(mock_http_client, "some_cookie")

        headers = mock_http_client.get.call_args.kwargs["headers"]
        assert "Mozilla/5.0" in headers["User-Agent"]
        assert "Chrome" in headers["User-Agent"]
