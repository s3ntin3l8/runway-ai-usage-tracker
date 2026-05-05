import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import httpx
import pytest

from app.services.collectors.chatgpt import ChatGPTCollector


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
        "email": "user@example.com",
        "rate_limit": {
            "primary_window": {
                "used_percent": 25.5,
                "reset_at": int((datetime.now(UTC) + timedelta(hours=3)).timestamp()),
            }
        },
    }


def _make_codex_jsonl_event(**kwargs) -> str:
    """Build a single Codex jsonl line for token_count or turn_context."""
    return json.dumps(kwargs)


def _make_codex_session_log() -> str:
    """Return mock Codex session log content with token_count events.

    Uses increasing input_tokens to model a single conversation where
    context grows.  Net-new input = last_input - first_input.
    """
    now = datetime.now(UTC)
    reset_at = int((now + timedelta(days=5)).timestamp())
    base = {
        "type": "token_count",
        "info": {
            "last_token_usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
                "reasoning_output_tokens": 20,
                "total_tokens": 1100,
            }
        },
        "rate_limits": {
            "primary": {
                "used_percent": 42.0,
                "window_minutes": 10080,
                "resets_at": reset_at,
                "plan_type": "plus",
            }
        },
    }
    lines = [
        _make_codex_jsonl_event(
            timestamp=now.isoformat().replace("+00:00", "Z"),
            type="turn_context",
            payload={"model": "gpt-5.4"},
        ),
        _make_codex_jsonl_event(
            timestamp=now.isoformat().replace("+00:00", "Z"),
            type="event_msg",
            payload={**base, "info": {"last_token_usage": {**base["info"]["last_token_usage"]}}},
        ),
        _make_codex_jsonl_event(
            timestamp=now.isoformat().replace("+00:00", "Z"),
            type="event_msg",
            payload={
                **base,
                "info": {
                    "last_token_usage": {
                        **base["info"]["last_token_usage"],
                        "input_tokens": 1500,
                        "cached_input_tokens": 300,
                        "output_tokens": 200,
                        "reasoning_output_tokens": 40,
                        "total_tokens": 1700,
                    }
                },
            },
        ),
        _make_codex_jsonl_event(
            timestamp=now.isoformat().replace("+00:00", "Z"),
            type="event_msg",
            payload={
                **base,
                "info": {
                    "last_token_usage": {
                        **base["info"]["last_token_usage"],
                        "input_tokens": 2000,
                        "cached_input_tokens": 400,
                        "output_tokens": 300,
                        "reasoning_output_tokens": 60,
                        "total_tokens": 2300,
                    }
                },
            },
        ),
    ]
    return "\n".join(lines)


class TestChatGPTCollectorDetailed:
    """Detailed unit tests for ChatGPTCollector's complex logic."""

    @pytest.mark.asyncio
    async def test_auth_priority_env(self, mock_http_client):
        """Priority 1: Environment variable should be used first."""
        collector = ChatGPTCollector()
        with patch.dict("os.environ", {"CHATGPT_OAUTH_TOKEN": "env_token"}):
            auth = await collector._get_auth_data(mock_http_client)
            assert auth["token"] == "env_token"
            assert auth["source"] == "api"
            assert auth["input_source"] == "server"

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
                    assert auth["source"] == "api"

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
                    "app.services.collectors.chatgpt_oauth.get_chatgpt_session_token",
                    return_value=session_token,
                ):
                    # Mock the refresh API call
                    mock_resp = MagicMock(spec=httpx.Response)
                    mock_resp.status_code = 200
                    mock_resp.headers = {}
                    mock_resp.json.return_value = {"accessToken": refreshed_token}
                    # http_request_with_retry uses request()
                    mock_http_client.request.return_value = mock_resp

                    auth = await collector._get_auth_data(mock_http_client)

                    assert auth["token"] == refreshed_token
                    assert auth["source"] == "web"

                    # Verify refresh URL was called with cookie
                    call_args = mock_http_client.request.call_args
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
        mock_resp.headers = {}
        mock_resp.json.return_value = {"accessToken": "t1"}
        mock_http_client.request.return_value = mock_resp

        # Patch EVERYTHING to ensure we hit the cookie path
        with patch.dict("os.environ", {}, clear=True):
            with patch("os.path.exists", return_value=False):
                with patch(
                    "app.services.collectors.chatgpt_oauth.get_chatgpt_session_token",
                    return_value=session_token,
                ):
                    # First refresh
                    auth1 = await collector._get_auth_data(mock_http_client)
                    assert auth1["token"] == "t1"
                    assert mock_http_client.request.call_count == 1

                    # Second call immediately - should use in-memory cache
                    auth2 = await collector._get_auth_data(mock_http_client)
                    assert auth2["token"] == "t1"
                    assert auth2["source"] == "web"
                    assert mock_http_client.request.call_count == 1  # No new API call

    @pytest.mark.asyncio
    async def test_tier_detection_plus(self, mock_http_client, chatgpt_usage_response):
        """Verify 'PLUS' tier detection from unified usage data."""
        collector = ChatGPTCollector()

        usage_resp = MagicMock(spec=httpx.Response)
        usage_resp.status_code = 200
        usage_resp.headers = {}
        usage_resp.json.return_value = chatgpt_usage_response  # contains "plus"

        mock_http_client.request.side_effect = [usage_resp]

        # Isolation
        with patch.dict("os.environ", {"CHATGPT_OAUTH_TOKEN": "token"}):
            results = await collector.collect(mock_http_client)

            # Consolidated into a single usage card
            assert len(results) == 1
            assert results[0]["service_name"] == "ChatGPT"
            assert results[0].get("variant") == "Codex"
            assert "PLUS" in results[0]["detail"]

    @pytest.mark.asyncio
    async def test_tier_detection_free(self, mock_http_client, chatgpt_usage_response):
        """Verify 'FREE' tier detection from unified usage data."""
        collector = ChatGPTCollector()

        # Modify fixture to be free for this specific test
        free_usage = chatgpt_usage_response.copy()
        free_usage["plan_type"] = "free"

        usage_resp = MagicMock(spec=httpx.Response)
        usage_resp.status_code = 200
        usage_resp.headers = {}
        usage_resp.json.return_value = free_usage

        mock_http_client.request.side_effect = [usage_resp]

        with patch.dict("os.environ", {"CHATGPT_OAUTH_TOKEN": "token"}):
            results = await collector.collect(mock_http_client)
            assert len(results) == 1
            assert "FREE" in results[0]["detail"]

    @pytest.mark.asyncio
    async def test_tier_extraction_from_usage_on_account_fail(
        self, mock_http_client, chatgpt_usage_response
    ):
        """Verify tier is extracted correctly from single usage call."""
        collector = ChatGPTCollector()

        # Mock usage success with "plus" tier
        usage_resp = MagicMock(spec=httpx.Response)
        usage_resp.status_code = 200
        usage_resp.headers = {}
        usage_resp.json.return_value = chatgpt_usage_response

        # Now only 1 call is needed
        mock_http_client.request.side_effect = [usage_resp]

        with patch.dict("os.environ", {"CHATGPT_OAUTH_TOKEN": "token"}):
            results = await collector.collect(mock_http_client)

            # Should have the Codex card with PLUS tier
            assert any(r.get("variant") == "Codex" for r in results)
            codex_card = next(r for r in results if r.get("variant") == "Codex")
            assert codex_card["tier"] == "plus"

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local strategy moved to sidecar")
    async def test_local_enrichment_injects_tokens(self, mock_http_client):
        """Verify local enrichment injects token_usage into primary Codex card."""
        collector = ChatGPTCollector()

        # Mock web API success
        usage_resp = MagicMock(spec=httpx.Response)
        usage_resp.status_code = 200
        usage_resp.headers = {}
        usage_resp.json.return_value = {
            "plan_type": "plus",
            "email": "user@example.com",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 30.0,
                    "reset_at": int((datetime.now(UTC) + timedelta(hours=3)).timestamp()),
                }
            },
        }
        mock_http_client.request.side_effect = [usage_resp]

        log_content = _make_codex_session_log()

        with (
            patch.dict("os.environ", {"CHATGPT_OAUTH_TOKEN": "token"}),
            patch(
                "app.services.collectors.chatgpt_local.glob.glob",
                return_value=["/fake/sessions/rollout.jsonl"],
            ),
            patch("os.path.isdir", return_value=True),
            patch("builtins.open", mock_open(read_data=log_content)),
        ):
            results = await collector.collect(mock_http_client)

            codex_card = next((r for r in results if r.get("variant") == "Codex"), None)
            assert codex_card is not None
            assert "token_usage" in codex_card
            # Total Consumption (sum of interactions, not context growth)
            assert codex_card["token_usage"]["input"] == 4500  # 1000 + 1500 + 2000
            assert codex_card["token_usage"]["output"] == 600  # 100 + 200 + 300
            assert codex_card["token_usage"]["reasoning"] == 120  # 20 + 40 + 60
            assert codex_card["token_usage"]["cache_read"] == 900  # 200 + 300 + 400
            assert codex_card["token_usage"]["total"] == 5100  # input + output
            assert codex_card["msgs"] == 3
            assert "by_model" in codex_card
            assert codex_card["by_model"]["gpt-5.4"]["msgs"] == 3
            assert "pct_used" in codex_card
            # Detail should show in: and out: totals
            assert "in:4.5k" in codex_card["detail"]
            assert "out:600" in codex_card["detail"]

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local strategy moved to sidecar")
    async def test_local_enrichment_does_not_fallback(self, mock_http_client):
        """Enrichment must not act as fallback when all primaries fail."""
        collector = ChatGPTCollector()

        # Mock API failure
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 429
        mock_resp.headers = {}
        mock_http_client.request.return_value = mock_resp

        log_content = _make_codex_session_log()

        with (
            patch.dict("os.environ", {"CHATGPT_OAUTH_TOKEN": "token"}),
            patch(
                "app.services.collectors.chatgpt.ChatGPTCollector._collect_via_cli_rpc",
                return_value=[],
            ),
            patch(
                "app.services.collectors.chatgpt_local.glob.glob",
                return_value=["/fake/sessions/rollout.jsonl"],
            ),
            patch("os.path.isdir", return_value=True),
            patch("builtins.open", mock_open(read_data=log_content)),
        ):
            results = await collector.collect(mock_http_client)

            # Error card remains; enrichment does not promote fallback
            assert len(results) == 1
            assert results[0]["remaining"] == "ERR"

    @pytest.mark.asyncio
    async def test_user_agent_on_auth_refresh(self, mock_http_client):
        """Check that a Chrome-like User-Agent is used for auth session refresh."""
        collector = ChatGPTCollector()

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = {"accessToken": "new_token"}
        mock_http_client.request.return_value = mock_resp

        await collector._refresh_access_token(mock_http_client, "some_cookie")

        # http_request_with_retry uses request()
        headers = mock_http_client.request.call_args.kwargs["headers"]
        assert "Mozilla/5.0" in headers["User-Agent"]
        assert "Chrome" in headers["User-Agent"]

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local strategy moved to sidecar")
    async def test_collect_via_cli_rpc_success(self):
        """Test successful data collection via codex CLI RPC."""
        collector = ChatGPTCollector()

        # We need a process-like object that behaves correctly
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdin.drain = AsyncMock()  # Must be awaitable
        mock_process.stdout = AsyncMock()
        mock_process.terminate = MagicMock()  # Sync mock
        mock_process.wait = AsyncMock()

        # Define mock responses for the 3 RPC calls (matching actual CLI output)
        mock_responses = [
            # initialize
            json.dumps({"jsonrpc": "2.0", "id": "1", "result": {"userAgent": "Test"}}).encode()
            + b"\n",
            # account/read
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "2",
                    "result": {"account": {"email": "test@example.com", "planType": "plus"}},
                }
            ).encode()
            + b"\n",
            # account/rateLimits/read
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "3",
                    "result": {
                        "rateLimits": {
                            "primary": {"usedPercent": 40.0, "resetsAt": 1744876800},
                            "credits": {"balance": 15.50, "unlimited": False},
                        }
                    },
                }
            ).encode()
            + b"\n",
            b"",  # End of stream
        ]

        mock_process.stdout.readline.side_effect = mock_responses

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            results = await collector._collect_via_cli_rpc()

            assert len(results) == 3
            # Check Account card
            assert results[0]["service_name"] == "ChatGPT"
            assert results[0].get("variant") is None
            assert results[0]["remaining"] == "PLUS"
            assert "test@example.com" in results[0]["detail"]
            # Check Codex card
            assert results[1]["service_name"] == "ChatGPT"
            assert results[1].get("variant") == "Codex"
            assert results[1]["remaining"] == "60.0%"
            assert results[1]["data_source"] == "local"
            # Check Credits card
            assert results[2]["service_name"] == "ChatGPT"
            assert results[2].get("variant") == "Credits"
            assert results[2]["remaining"] == "$15.50"
