"""
Unit tests for quota collectors.

Tests cover:
- OAuth/API collection success and error handling
- Fallback logic between primary and secondary sources
- Token caching and refresh behavior
- Error card generation for various failure scenarios
- Local log parsing and file-based data sources
"""

import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import httpx
import pytest

from app.services.collectors.anthropic import AnthropicCollector
from app.services.collectors.antigravity import AntigravityCollector
from app.services.collectors.chatgpt import ChatGPTCollector
from app.services.collectors.gemini import GeminiCollector
from app.services.collectors.github import GitHubCollector
from app.services.collectors.kimi_api import KimiApiCollector
from app.services.collectors.kimi_coding import KimiCodingCollector
from app.services.collectors.kimi_k2 import KimiK2Collector
from app.services.collectors.minimax import MiniMaxCollector
from app.services.collectors.opencode import OpenCodeCollector
from app.services.collectors.openrouter import OpenRouterCollector
from app.services.collectors.zai import ZaiCollector


class TestAnthropicCollector:
    """Test suite for Anthropic (Claude) collector."""

    @pytest.mark.asyncio
    async def test_collect_oauth_success(self, mock_http_client, mock_anthropic_oauth_response):
        """Test successful OAuth API collection."""
        collector = AnthropicCollector()

        # Mock successful OAuth response using request() (called by http_request_with_retry)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_anthropic_oauth_response
        mock_http_client.request.return_value = mock_response

        with (
            patch(
                "app.services.credential_provider.CredentialProvider.get_claude_token",
                return_value="test_token",
            ),
            patch("app.services.collectors.anthropic.settings") as mock_settings,
        ):
            mock_settings.CLAUDE_PROJECTS_DIR = "/home/user/.claude/projects"
            mock_settings.LOCAL_COLLECTOR_ENABLED = True
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
            mock_settings.CLAUDE_PRO_LIMIT = 2000000
            mock_settings.CLAUDE_FREE_LIMIT = 500000

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value=None,
                ),
                patch.object(collector, "_get_valid_token", return_value="test_token"),
            ):
                result = await collector.collect(mock_http_client)

        # Should return cards for each quota window
        assert isinstance(result, list)
        assert len(result) >= 1
        assert all("service_name" in card for card in result)
        assert any("Session" in str(card.get("service_name", "")) for card in result)

    @pytest.mark.asyncio
    async def test_refresh_token_lookup_is_scoped_to_account(self, mock_http_client):
        """Test that Anthropic refresh token fallback uses the collector account."""
        collector = AnthropicCollector(account_id="acc_a")

        with (
            patch.object(
                collector,
                "_get_credentials",
                new_callable=AsyncMock,
                return_value={"claudeAiOauth": {}},
            ),
            patch(
                "app.services.collectors.oauth_base.token_cache.get_token",
                new_callable=AsyncMock,
                return_value="refresh_a",
            ) as mock_get_token,
        ):
            response = MagicMock(spec=httpx.Response)
            response.status_code = 400
            response.json.return_value = {"error": "invalid_grant"}

            with patch(
                "app.services.collectors.anthropic_oauth.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=response,
            ):
                await collector._execute_refresh(mock_http_client)

        mock_get_token.assert_awaited_once_with("anthropic", "refresh_token", account_id="acc_a")

    @pytest.mark.asyncio
    async def test_collect_oauth_401_fallback(self, mock_http_client):
        """Test fallback to local logs when OAuth token is invalid (401)."""
        collector = AnthropicCollector()

        # Mock 401 response using request()
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 401
        mock_http_client.request.return_value = mock_response

        with (
            patch(
                "app.services.credential_provider.CredentialProvider.get_claude_token",
                return_value="invalid_token",
            ),
            patch("app.services.collectors.anthropic.settings") as mock_settings,
        ):
            mock_settings.CLAUDE_PRO_LIMIT = 2000000
            mock_settings.CLAUDE_FREE_LIMIT = 500000
            mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
            mock_settings.LOCAL_COLLECTOR_ENABLED = True
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value=None,
                ),
                patch.object(collector, "_get_valid_token", return_value="invalid_token"),
                patch(
                    "app.services.collectors.anthropic_local.glob.glob",
                    return_value=[],
                ),
            ):
                result = await collector.collect(mock_http_client)

        # Should return error card for invalid token (no logs fallback)
        assert any("ERR" in str(card.get("remaining", "")) for card in result)

    @pytest.mark.asyncio
    @patch("asyncio.sleep")
    async def test_collect_oauth_proactive_429_backoff(
        self, mock_sleep, mock_http_client, mock_anthropic_oauth_response
    ):
        """Test that 429 rate limit triggers proactive backoff for subsequent calls."""
        collector = AnthropicCollector()

        # Mock 429 rate limit response with a Retry-After header
        mock_429_response = MagicMock(spec=httpx.Response)
        mock_429_response.status_code = 429
        mock_429_response.headers = {"Retry-After": "60"}  # 60 seconds

        # After 3 retries (first call), it returns the 429
        mock_http_client.request.return_value = mock_429_response

        with (
            patch(
                "app.services.credential_provider.CredentialProvider.get_claude_token",
                return_value="test_token",
            ),
            patch("app.services.collectors.anthropic.settings") as mock_settings,
        ):
            mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
            mock_settings.LOCAL_COLLECTOR_ENABLED = False

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value="fake_session",
                ),
                patch.object(collector, "_get_valid_token", return_value="test_token"),
                patch.object(
                    collector,
                    "_get_claude_via_web_api",
                    return_value=[{"service_name": "Fallback", "data_source": "web_api"}],
                ),
            ):
                # First call - OAuth gets 429 with 60s Retry-After.
                # http_request_with_retry will abort retries because 60s > 10s cap.
                await collector.collect(mock_http_client)
                assert mock_http_client.request.call_count == 1

                # Second call - should skip OAuth entirely due to proactive backoff
                await collector.collect(mock_http_client)

                # Still 1 call, not 2 (OAuth was skipped proactively)
                assert mock_http_client.request.call_count == 1

    @pytest.mark.asyncio
    async def test_collect_anthropic_with_paid_usage(self, mock_http_client):
        """Test Claude collection with prepaid balance and overage spend limits."""
        collector = AnthropicCollector()

        # Mock OAuth response with balance and spend limit
        mock_paid_data = {
            "five_hour": {"utilization": 25.0, "resets_at": "2025-04-12T15:00:00Z"},
            "current_balance": 15.75,
            "extra_usage": {"spend": 2.50, "limit": 20.00},
        }

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_paid_data
        mock_http_client.request.return_value = mock_response

        with patch("app.services.collectors.anthropic.settings") as mock_settings:
            mock_settings.LOCAL_COLLECTOR_ENABLED = False

            with patch.object(collector, "_get_valid_token", return_value="test_token"):
                result = await collector.collect(mock_http_client)

        # Should return 6 cards: 4 standard quota windows + Balance + Extra Usage
        assert len(result) == 6

        services = {c["service_name"]: c for c in result}
        assert "Claude (Session Window)" in services
        assert "Claude (Current Balance)" in services
        assert "Claude (Extra Usage)" in services

        # Check Balance card
        bal_card = services["Claude (Current Balance)"]
        assert bal_card["remaining"] == "$15.75"
        assert bal_card["unit"] == "USD"
        assert bal_card["icon"] == "💰"

        # Check Extra Usage card
        extra_card = services["Claude (Extra Usage)"]
        assert extra_card["remaining"] == "$17.50"  # 20.00 - 2.50
        assert extra_card["unit"] == "limit"
        assert "Spent: $2.50" in extra_card["detail"]

    @pytest.mark.asyncio
    async def test_collect_oauth_success_clears_backoff(
        self, mock_http_client, mock_anthropic_oauth_response
    ):
        """Test that a successful call clears any previous 429 backoff."""
        collector = AnthropicCollector()

        # Mock success response
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_anthropic_oauth_response
        mock_response.headers = {}
        mock_http_client.request.return_value = mock_response

        # Manually set a backoff from the past
        collector._last_429_backoff_until = datetime.now(UTC) - timedelta(minutes=1)

        with patch.object(collector, "_get_valid_token", return_value="test_token"):
            await collector.collect(mock_http_client)

            # Backoff should be cleared on success
            assert collector._last_429_backoff_until is None

    @pytest.mark.asyncio
    async def test_collect_oauth_token_refresh_success(
        self, mock_http_client, mock_anthropic_oauth_response
    ):
        """Test successful OAuth token refresh when original token is expired."""
        collector = AnthropicCollector()

        # Mock successful token refresh response
        refresh_response = MagicMock(spec=httpx.Response)
        refresh_response.status_code = 200
        refresh_response.headers = {}
        refresh_response.json.return_value = {
            "access_token": "new_refreshed_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 28800,
        }

        # Control the flow by mocking _get_valid_token and _get_claude_oauth
        with patch.object(collector, "_get_valid_token", return_value="refreshed_token"):
            with patch.object(collector, "_get_claude_oauth") as mock_get_oauth:
                # First call returns 401 error card, second (after refresh) returns success
                mock_get_oauth.side_effect = [
                    [
                        {
                            "service_name": "Claude Pro",
                            "remaining": "ERR",
                            "detail": "Expired/Invalid Token (OAuth)",
                        }
                    ],
                    [{"service_name": "Claude", "remaining": "50%", "data_source": "oauth"}],
                ]

            # http_request_with_retry uses request()
            mock_http_client.request.return_value = refresh_response

            with patch(
                "app.services.credential_provider.CredentialProvider.get_claude_token",
                return_value="expired_token",
            ):
                with patch("app.services.collectors.anthropic.settings") as mock_settings:
                    mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
                    mock_settings.CLAUDE_PRO_LIMIT = 2000000
                    mock_settings.CLAUDE_FREE_LIMIT = 500000
                    mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
                    mock_settings.LOCAL_COLLECTOR_ENABLED = False

                    # Return False for expiration check so we hit the reactive path (401 response)
                    with patch.object(collector, "_is_token_expired", return_value=False):
                        # Mock refresh token availability
                        with patch.object(
                            collector,
                            "_get_credentials",
                            return_value={"claudeAiOauth": {"refreshToken": "valid_refresh_token"}},
                        ):
                            with patch.object(collector, "_persist_credentials", return_value=None):
                                with patch(
                                    "app.services.token_cache.token_cache.store",
                                    return_value=None,
                                ):
                                    # First request gets 401, then reactive refresh happens, then second request succeeds
                                    result = await collector.collect(mock_http_client)

        # Should return successful OAuth results (not error cards)
        assert isinstance(result, list)
        assert len(result) >= 1
        assert all(card.get("remaining") != "ERR" for card in result)
        assert any(card.get("data_source") == "oauth" for card in result)

    @pytest.mark.asyncio
    async def test_collect_web_api_fallback(
        self,
        mock_http_client,
        mock_claude_web_api_orgs_response,
        mock_claude_web_api_usage_response,
    ):
        """Test fallback to Web API when OAuth fails."""
        collector = AnthropicCollector()

        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401
        oauth_response.headers = {}

        # Mock Web API success
        orgs_response = MagicMock(spec=httpx.Response)
        orgs_response.status_code = 200
        orgs_response.headers = {}
        orgs_response.json.return_value = mock_claude_web_api_orgs_response

        # Mock account endpoint
        account_response = MagicMock(spec=httpx.Response)
        account_response.status_code = 200
        account_response.headers = {}
        account_response.json.return_value = {"tier": "pro"}

        usage_response = MagicMock(spec=httpx.Response)
        usage_response.status_code = 200
        usage_response.headers = {}
        usage_response.json.return_value = mock_claude_web_api_usage_response

        # Mock request for all calls (OAuth + Web API)
        mock_http_client.request.side_effect = [
            oauth_response,
            orgs_response,
            account_response,
            usage_response,
        ]

        with (
            patch(
                "app.services.credential_provider.CredentialProvider.get_claude_token",
                return_value="invalid_token",
            ),
            patch("app.services.collectors.anthropic.settings") as mock_settings,
        ):
            mock_settings.CLAUDE_PRO_LIMIT = 2000000
            mock_settings.CLAUDE_FREE_LIMIT = 500000
            mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
            mock_settings.LOCAL_COLLECTOR_ENABLED = False

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value="sk-ant-session123",
                ),
                patch.object(collector, "_get_valid_token", return_value="invalid_token"),
            ):
                result = await collector.collect(mock_http_client)

        # Should return Web API results — all 4 core windows are always emitted
        assert isinstance(result, list)
        assert len(result) == 4
        services = [r["service_name"] for r in result]
        assert "Claude (Session Window)" in services
        assert "Claude (Weekly Window)" in services
        assert any(card.get("data_source") == "web_api" for card in result)

    @pytest.mark.asyncio
    async def test_collect_enhanced_local_fallback(self, mock_http_client):
        """Test fallback to enhanced local logs when both OAuth and Web API fail."""
        collector = AnthropicCollector()

        # Mock OAuth failure - OAuth uses request() not get()
        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401
        mock_http_client.request.return_value = oauth_response

        # Mock no web cookie
        with (
            patch(
                "app.services.credential_provider.CredentialProvider.get_claude_token",
                return_value="invalid_token",
            ),
            patch("app.services.collectors.anthropic.settings") as mock_settings,
        ):
            mock_settings.CLAUDE_PRO_LIMIT = 2000000
            mock_settings.CLAUDE_FREE_LIMIT = 500000
            mock_settings.LOCAL_COLLECTOR_ENABLED = True
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value=None,
                ),
                patch.object(collector, "_get_valid_token", return_value="invalid_token"),
            ):
                # Mock local log data with all token types
                log_data = [
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": datetime.now(UTC).isoformat(),
                            "message": {
                                "id": "msg_1",
                                "requestId": "req_1",
                                "usage": {
                                    "input_tokens": 1000,
                                    "output_tokens": 500,
                                    "cache_read_tokens": 2000,
                                    "cache_creation_tokens": 100,
                                },
                            },
                        }
                    )
                    + "\n",
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": datetime.now(UTC).isoformat(),
                            "message": {
                                "id": "msg_2",  # Different message, should be counted
                                "requestId": "req_2",
                                "usage": {
                                    "input_tokens": 500,
                                    "output_tokens": 200,
                                    "cache_read_tokens": 0,
                                    "cache_creation_tokens": 0,
                                },
                            },
                        }
                    )
                    + "\n",
                ]

                with (
                    patch("builtins.open", mock_open(read_data="".join(log_data))),
                    patch(
                        "app.services.collectors.anthropic_local.glob.glob",
                        return_value=["/fake/path/test.jsonl"],
                    ),
                    patch("os.path.isdir", return_value=True),
                    patch.object(collector, "_strategy_cli_pty", return_value=[]),
                ):
                    result = await collector.collect(mock_http_client)

        # Should return local log results
        assert isinstance(result, list)
        assert len(result) == 1
        assert "Claude Pro" in str(result[0].get("service_name", ""))
        assert "Local Logs" in str(result[0].get("detail", ""))
        # Should sum all token types: (1000+500+2000+100) + (500+200+0+0) = 4300
        assert "4,300" in str(result[0].get("detail", "")) or "4300" in str(
            result[0].get("detail", "")
        )

    @pytest.mark.asyncio
    async def test_collect_local_dedup(self, mock_http_client):
        """Test deduplication of streaming chunks in local logs."""
        collector = AnthropicCollector()

        # Mock OAuth failure - OAuth uses request() not get()
        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401
        mock_http_client.request.return_value = oauth_response

        # Mock local log data with duplicate messages (streaming chunks)
        log_data = [
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "message": {
                        "id": "msg_dup",
                        "requestId": "req_dup",
                        "usage": {
                            "input_tokens": 1000,
                            "output_tokens": 500,
                            "cache_read_tokens": 0,
                            "cache_creation_tokens": 0,
                        },
                    },
                }
            )
            + "\n",
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "message": {
                        "id": "msg_dup",  # Same ID - should be deduplicated
                        "requestId": "req_dup",  # Same requestId
                        "usage": {
                            "input_tokens": 1000,
                            "output_tokens": 500,
                            "cache_read_tokens": 0,
                            "cache_creation_tokens": 0,
                        },
                    },
                }
            )
            + "\n",
        ]

        with (
            patch(
                "app.services.credential_provider.CredentialProvider.get_claude_token",
                return_value="invalid_token",
            ),
            patch("app.services.collectors.anthropic.settings") as mock_settings,
        ):
            mock_settings.CLAUDE_PRO_LIMIT = 2000000
            mock_settings.CLAUDE_FREE_LIMIT = 500000
            mock_settings.LOCAL_COLLECTOR_ENABLED = True
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value=None,
                ),
                patch.object(collector, "_get_valid_token", return_value="invalid_token"),
                patch("builtins.open", mock_open(read_data="".join(log_data))),
                patch(
                    "app.services.collectors.anthropic_local.glob.glob",
                    return_value=["/fake/path/test.jsonl"],
                ),
                patch("os.path.isdir", return_value=True),
                patch.object(collector, "_strategy_cli_pty", return_value=[]),
            ):
                result = await collector.collect(mock_http_client)

        # Should deduplicate - only count once
        assert isinstance(result, list)
        assert len(result) == 1
        # Should only show 1500 tokens (not 3000 from duplicate)
        detail = str(result[0].get("detail", ""))
        assert "1,500" in detail or "1500" in detail

    @pytest.mark.asyncio
    async def test_collect_multi_config_dirs(self, mock_http_client):
        """Test scanning multiple config directories via CLAUDE_CONFIG_DIR."""
        collector = AnthropicCollector()

        # Mock OAuth failure - OAuth uses request() not get()
        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401
        mock_http_client.request.return_value = oauth_response

        with (
            patch(
                "app.services.credential_provider.CredentialProvider.get_claude_token",
                return_value="invalid_token",
            ),
            patch("app.services.collectors.anthropic.settings") as mock_settings,
        ):
            mock_settings.CLAUDE_PRO_LIMIT = 2000000
            mock_settings.CLAUDE_FREE_LIMIT = 500000
            mock_settings.LOCAL_COLLECTOR_ENABLED = True
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value=None,
                ),
                patch.object(collector, "_get_valid_token", return_value="invalid_token"),
                patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": "/path1,/path2"}),
                patch("os.path.isdir", return_value=True),
                patch.object(collector, "_strategy_cli_pty", return_value=[]),
                patch("app.services.collectors.anthropic_local.glob.glob") as mock_glob,
            ):
                # Return files from both paths
                def glob_side_effect(pattern, **kwargs):
                    if "/path1" in pattern:
                        return ["/path1/projects/file1.jsonl"]
                    if "/path2" in pattern:
                        return ["/path2/projects/file2.jsonl"]
                    return []

                mock_glob.side_effect = glob_side_effect

                # Mock file contents
                log_data_1 = (
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": datetime.now(UTC).isoformat(),
                            "message": {
                                "id": "msg_1",
                                "requestId": "req_1",
                                "usage": {
                                    "input_tokens": 1000,
                                    "output_tokens": 500,
                                },
                            },
                        }
                    )
                    + "\n"
                )

                log_data_2 = (
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": datetime.now(UTC).isoformat(),
                            "message": {
                                "id": "msg_2",
                                "requestId": "req_2",
                                "usage": {
                                    "input_tokens": 500,
                                    "output_tokens": 200,
                                },
                            },
                        }
                    )
                    + "\n"
                )

                def open_side_effect(path, **kwargs):
                    if "file1" in path:
                        return mock_open(read_data=log_data_1)()
                    return mock_open(read_data=log_data_2)()

                with patch("builtins.open", side_effect=open_side_effect):
                    result = await collector.collect(mock_http_client)

        # Should aggregate from both directories
        assert isinstance(result, list)
        assert len(result) == 1

    def test_extract_identity_from_oauth(self):
        """Test identity extraction from OAuth API response."""
        collector = AnthropicCollector()

        # Full identity
        data_full = {"account": {"email": "user@example.com", "organization": "test-org"}}
        identity = collector._extract_identity_from_oauth(data_full)
        assert identity == "user@example.com @ test-org"

        # Email only
        data_email = {"account": {"email": "user@example.com"}}
        identity = collector._extract_identity_from_oauth(data_email)
        assert identity == "user@example.com"

        # Org only
        data_org = {"account": {"organization": "test-org"}}
        identity = collector._extract_identity_from_oauth(data_org)
        assert identity == "org: test-org"

        # No identity
        data_empty = {"account": {}}
        identity = collector._extract_identity_from_oauth(data_empty)
        assert identity == ""

        # Missing account key
        data_missing = {}
        identity = collector._extract_identity_from_oauth(data_missing)
        assert identity == ""

    def test_extract_identity_from_web(self):
        """Test identity extraction from Web API response."""
        collector = AnthropicCollector()

        # Full identity
        org_data = {
            "name": "Test Org",
            "membership": {"user": {"email": "user@example.com"}},
        }
        identity = collector._extract_identity_from_web(org_data)
        assert identity == "user@example.com @ Test Org"

        # Email only
        org_email = {"membership": {"user": {"email": "user@example.com"}}}
        identity = collector._extract_identity_from_web(org_email)
        assert identity == "user@example.com"

        # Org name only
        org_name = {"name": "Test Org"}
        identity = collector._extract_identity_from_web(org_name)
        assert identity == "org: Test Org"

        # Empty
        org_empty = {}
        identity = collector._extract_identity_from_web(org_empty)
        assert identity == ""

    @pytest.mark.asyncio
    async def test_collect_oauth_with_identity_in_detail(self, mock_http_client):
        """Test that OAuth response includes identity in detail field."""
        collector = AnthropicCollector()

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "five_hour": {"utilization": 25.0, "resets_at": "2025-04-07T12:00:00Z"},
            "account": {"email": "test@example.com", "organization": "test-org"},
        }
        mock_http_client.request.return_value = mock_response

        with (
            patch(
                "app.services.credential_provider.CredentialProvider.get_claude_token",
                return_value="test_token",
            ),
            patch("app.services.collectors.anthropic.settings") as mock_settings,
        ):
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
            mock_settings.LOCAL_COLLECTOR_ENABLED = False
            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value=None,
                ),
                patch.object(collector, "_get_valid_token", return_value="test_token"),
            ):
                result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) >= 1
        # Check detail includes identity
        detail = result[0].get("detail", "")
        assert "test@example.com" in detail
        assert "test-org" in detail
        assert "[OAuth]" in detail

    @pytest.mark.asyncio
    async def test_collect_web_api_with_identity(self, mock_http_client):
        """Test that Web API response includes identity in detail field."""
        collector = AnthropicCollector()

        # Mock OAuth to fail so we fall back to Web API
        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401

        # Mock Web API org response with identity
        org_response = MagicMock(spec=httpx.Response)
        org_response.status_code = 200
        org_response.json.return_value = [
            {
                "uuid": "org_123",
                "name": "Personal Org",
                "membership": {"user": {"email": "user@example.com"}},
            }
        ]

        # Mock account endpoint (optional, called between orgs and usage)
        account_response = MagicMock(spec=httpx.Response)
        account_response.status_code = 200
        account_response.headers = {}
        account_response.json.return_value = {"tier": "pro"}

        usage_response = MagicMock(spec=httpx.Response)
        usage_response.status_code = 200
        usage_response.headers = {}
        usage_response.json.return_value = {
            "five_hour": {"utilization": 0.3, "resets_at": "2025-04-07T12:00:00Z"},
            "seven_day": {"utilization": 0.4, "resets_at": "2025-04-14T00:00:00Z"},
            "seven_day_sonnet": {"utilization": 0.5, "resets_at": "2025-04-14T00:00:00Z"},
        }

        mock_http_client.request.side_effect = [
            oauth_response,
            org_response,
            account_response,
            usage_response,
        ]

        with (
            patch(
                "app.services.credential_provider.CredentialProvider.get_claude_token",
                return_value="invalid_token",
            ),
            patch("app.services.collectors.anthropic.settings") as mock_settings,
        ):
            mock_settings.CLAUDE_PRO_LIMIT = 2000000
            mock_settings.CLAUDE_FREE_LIMIT = 500000
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
            mock_settings.LOCAL_COLLECTOR_ENABLED = False

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value="session_key",
                ),
                patch.object(collector, "_get_valid_token", return_value="invalid_token"),
            ):
                result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) == 4  # all 4 core windows always emitted
        assert any(card.get("data_source") == "web_api" for card in result)

        # Identity should be included in detail
        for card in result:
            detail = card.get("detail", "")
            assert "user@example.com" in detail or "Personal Org" in detail

    def test_parse_oauth_response_boundary_percentages(self):
        """Test boundary percentage handling (0%, 100%)."""
        collector = AnthropicCollector()

        # 0% used (100% remaining)
        data_zero = {"five_hour": {"utilization": 0.0, "resets_at": "2025-04-07T12:00:00Z"}}
        result = collector._parse_oauth_response(data_zero, {"five_hour": "Session Window"})
        assert result[0]["remaining"] == "100.0%"
        assert result[0]["health"] == "good"

        # 100% used (0% remaining)
        data_full = {"five_hour": {"utilization": 100.0, "resets_at": "2025-04-07T12:00:00Z"}}
        result = collector._parse_oauth_response(data_full, {"five_hour": "Session Window"})
        assert result[0]["remaining"] == "0.0%"
        assert result[0]["health"] == "critical"

    def test_parse_oauth_response_invalid_timestamp(self):
        """Test graceful handling of invalid reset timestamps."""
        collector = AnthropicCollector()

        data = {"five_hour": {"utilization": 25.0, "resets_at": "invalid-timestamp"}}
        result = collector._parse_oauth_response(data, {"five_hour": "Session Window"})

        # Should not crash, should return card with reset as "—"
        # Now returns 4 items because core windows are guaranteed
        assert isinstance(result, list)
        assert len(result) == 4
        assert result[0]["reset"] == "—"

    def test_parse_oauth_response_empty_windows(self):
        """Test handling when no valid quota windows present."""
        collector = AnthropicCollector()

        # Empty data - should default to 100% remaining for guaranteed windows
        result = collector._parse_oauth_response({}, {"five_hour": "Session Window"})
        assert result[0]["remaining"] == "100.0%"

        # Data without utilization field - should default to 100% remaining for guaranteed windows
        data_no_util = {"five_hour": {"resets_at": "2025-04-07T12:00:00Z"}}
        result = collector._parse_oauth_response(data_no_util, {"five_hour": "Session Window"})
        assert result[0]["remaining"] == "100.0%"

    @pytest.mark.asyncio
    async def test_get_claude_local_enhanced_uses_to_thread(self):
        """C5: _get_claude_local_enhanced must delegate sync I/O to asyncio.to_thread."""

        collector = AnthropicCollector()

        with patch("app.services.collectors.anthropic_local.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value=None)
            with patch("app.services.collectors.anthropic_local.settings") as mock_settings:
                mock_settings.CLAUDE_PRO_LIMIT = 2000000
                mock_settings.CLAUDE_FREE_LIMIT = 500000
                mock_settings.CLAUDE_PROJECTS_DIR = ""

                result = await collector._get_claude_local_enhanced()

        mock_asyncio.to_thread.assert_called_once()
        called_fn = mock_asyncio.to_thread.call_args[0][0]
        assert callable(called_fn), "asyncio.to_thread must be called with a callable"

    def test_tier_mapping_from_creds(self):
        """Test that user plan is correctly inferred from rate_limit_tier in credentials."""
        collector = AnthropicCollector()
        name_map = {"five_hour": "Session Window"}
        data = {"five_hour": {"utilization": 10.0}}

        # Test Tier 1 -> Pro
        creds_pro = {"claudeAiOauth": {"rateLimitTier": "tier_1"}}
        result = collector._parse_oauth_response(data, name_map, creds_pro)
        assert result[0]["tier"] == "Pro"

        # Test Tier 2 -> Max
        creds_max = {"claudeAiOauth": {"rateLimitTier": "tier_2"}}
        result = collector._parse_oauth_response(data, name_map, creds_max)
        assert result[0]["tier"] == "Max"

        # Test Tier 3 -> Team
        creds_team = {"claudeAiOauth": {"rateLimitTier": "tier_3"}}
        result = collector._parse_oauth_response(data, name_map, creds_team)
        assert result[0]["tier"] == "Team"

        # Test Tier 0 -> Free
        creds_free = {"claudeAiOauth": {"rateLimitTier": "tier_0"}}
        result = collector._parse_oauth_response(data, name_map, creds_free)
        assert result[0]["tier"] == "Free"

        # Fallback to API plan
        data_with_plan = {"account": {"plan": "plus"}, "five_hour": {"utilization": 10.0}}
        result = collector._parse_oauth_response(data_with_plan, name_map, None)
        assert result[0]["tier"] == "Plus"

    @pytest.mark.asyncio
    async def test_collect_via_cli_pty_success(self):
        """Test successful parsing of 'claude /usage' output including ANSI codes."""
        collector = AnthropicCollector()

        # Mock ANSI-rich output
        raw_output = (
            b"\x1b[1mCurrent session\x1b[0m: \x1b[32m12.5%\x1b[0m used (resets in 2h 30m)\n"
            b"\x1b[1mCurrent week\x1b[0m: \x1b[32m5.0%\x1b[0m used (resets in 4d 12h)\n"
        )

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0

        mock_cli = AsyncMock()
        mock_cli.communicate = AsyncMock(return_value=(raw_output, b""))

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            # First call: which claude -> success
            # Second call: claude -> returns output
            mock_exec.side_effect = [mock_proc, mock_cli]

            result = await collector._collect_via_cli_pty()

        assert len(result) == 2
        # Check Session card
        assert "Session Window" in result[0]["service_name"]
        assert result[0]["used_value"] == 12.5
        assert result[0]["remaining"] == "87.5%"
        assert result[0]["data_source"] == "cli"
        assert "[CLI PTY]" in result[0]["detail"]

        # Check Weekly card
        assert "Weekly Window" in result[1]["service_name"]
        assert result[1]["used_value"] == 5.0
        assert result[1]["remaining"] == "95.0%"
        assert "4d" in result[1]["reset"]


class TestGeminiCollector:
    """Test suite for Google Gemini collector."""

    @pytest.mark.asyncio
    async def test_collect_api_success(self, mock_http_client, mock_gemini_quota_response):
        """Test successful Gemini API collection with project discovery."""
        collector = GeminiCollector()

        # Mock responses - tier request comes FIRST (to get project ID)
        tier_response = MagicMock(spec=httpx.Response)
        tier_response.status_code = 200
        tier_response.headers = {}
        tier_response.json.return_value = {
            "currentTier": {"id": "standard-tier", "name": "Gemini Code Assist"},
            "cloudaicompanionProject": "test-project-123",
        }

        quota_response = MagicMock(spec=httpx.Response)
        quota_response.status_code = 200
        quota_response.headers = {}
        quota_response.json.return_value = mock_gemini_quota_response

        # Create async mock that returns responses in order
        call_count = [0]

        async def mock_request(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return tier_response  # First call: loadCodeAssist
            return quota_response  # Second call: retrieveUserQuota

        mock_http_client.request = mock_request

        with patch("app.services.collectors.gemini.settings") as mock_settings:
            mock_settings.GEMINI_OAUTH_PATH = "/fake/creds.json"
            mock_settings.GEMINI_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with (
                patch(
                    "builtins.open",
                    mock_open(
                        read_data=json.dumps(
                            {"access_token": "token", "expiry_date": 9999999999999}
                        )
                    ),
                ),
                patch("app.services.collectors.oauth_base.os.path.exists", return_value=True),
                patch("app.services.collectors.gemini_oauth.time.time", return_value=1000),
            ):
                result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) >= 1
        # Should return one card per model family
        assert len(result) <= len(mock_gemini_quota_response["buckets"])
        # Check that service name contains model identifier (either display name or raw model ID)
        assert any(name in str(result[0].get("service_name", "")) for name in ["Gemini", "gemini"])
        # Verify health field exists
        assert "health" in result[0]
        # Verify unit is "used" (not "quota")
        assert result[0].get("unit") == "used"
        # Verify project was used in quota call
        assert call_count[0] == 2  # Should make 2 API calls

    @pytest.mark.asyncio
    async def test_collect_api_with_absolute_quota(self, mock_http_client):
        """Test Gemini API collection with absolute quota fields (quotaLimit, quotaRemaining)."""
        collector = GeminiCollector()

        # Mock responses
        tier_response = MagicMock(spec=httpx.Response)
        tier_response.status_code = 200
        tier_response.json.return_value = {
            "currentTier": {"id": "standard-tier", "name": "Gemini Code Assist"},
            "cloudaicompanionProject": "test-project-123",
        }

        quota_response = MagicMock(spec=httpx.Response)
        quota_response.status_code = 200
        quota_response.json.return_value = {
            "buckets": [
                {
                    "modelId": "gemini-1.5-pro",
                    "remainingFraction": 0.85,
                    "resetTime": "2025-04-08T00:00:00Z",
                    "quotaLimit": 1000,
                    "quotaRemaining": 850,
                    "tokenType": "REQUEST",
                }
            ]
        }

        async def mock_request(*args, **kwargs):
            if "loadCodeAssist" in args[1]:
                return tier_response
            return quota_response

        mock_http_client.request = mock_request

        with patch("app.services.collectors.gemini_oauth.settings") as mock_settings:
            mock_settings.GEMINI_OAUTH_PATH = "/fake/creds.json"
            mock_settings.GEMINI_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with (
                patch(
                    "builtins.open",
                    mock_open(
                        read_data=json.dumps(
                            {"access_token": "token", "expiry_date": 9999999999999}
                        )
                    ),
                ),
                patch("app.services.collectors.oauth_base.os.path.exists", return_value=True),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        card = result[0]
        # Primary remaining should still be %
        assert card["remaining"] == "15%"  # 100 - (0.85*100)
        # Detail should contain absolute numbers
        assert "850 / 1,000 request left" in card["detail"]
        # used_value and limit_value should be absolute
        assert card["used_value"] == 150.0  # 1000 - 850
        assert card["limit_value"] == 1000.0
        assert card["unit_type"] == "request"

    @pytest.mark.asyncio
    async def test_collect_missing_credentials(self, mock_http_client):
        """Test graceful handling when credentials file missing."""
        collector = GeminiCollector()

        with patch("app.services.collectors.gemini_oauth.settings") as mock_settings:
            mock_settings.GEMINI_OAUTH_PATH = "/fake/missing.json"
            mock_settings.GEMINI_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with patch("app.services.collectors.oauth_base.os.path.exists", return_value=False):
                result = await collector.collect(mock_http_client)

        # Should return empty list or fallback to logs
        assert isinstance(result, list)

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Caching removed by T2")
    async def test_collect_api_error_caching(self, mock_http_client):
        """Test that API results are cached to avoid hammering the API (same instance)."""
        from unittest.mock import AsyncMock

        collector = GeminiCollector()

        # Verify initial state - no cache
        assert collector._cached_results is None
        assert collector._last_fetch is None

        # Mock API error response (429 rate limit)
        error_response = MagicMock(spec=httpx.Response)
        error_response.status_code = 429
        error_response.headers = {}

        # Use AsyncMock to track calls
        mock_request = AsyncMock(return_value=error_response)
        mock_http_client.request = mock_request

        with patch("app.services.collectors.gemini.settings") as mock_settings:
            mock_settings.GEMINI_OAUTH_PATH = "/fake/creds.json"
            mock_settings.GEMINI_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with (
                patch(
                    "builtins.open",
                    mock_open(
                        read_data=json.dumps(
                            {"access_token": "token", "expiry_date": 9999999999999}
                        )
                    ),
                ),
                patch("app.services.collectors.gemini_oauth.os.path.exists", return_value=True),
                patch("app.services.collectors.gemini_oauth.time.time", return_value=1000),
            ):
                # First call - API fails
                result1 = await collector.collect(mock_http_client)
                first_call_count = mock_request.call_count

                # Verify cache was populated (any result)
                assert collector._cached_results is not None
                assert collector._last_fetch is not None

                # Second call with SAME collector instance - should use cache
                result2 = await collector.collect(mock_http_client)

                # API should not be called again (result was cached)
                assert mock_request.call_count == first_call_count

                # Results should be the same (from cache, ignoring timestamp)
                assert len(result1) == len(result2)
                for r1, r2 in zip(result1, result2):
                    r1_copy = r1.copy()
                    r2_copy = r2.copy()
                    r1_copy.pop("updated_at", None)
                    r2_copy.pop("updated_at", None)
                    assert r1_copy == r2_copy


class TestGitHubCollector:
    """Test suite for GitHub Copilot collector."""

    @pytest.mark.asyncio
    async def test_collect_free_tier_quotas(self, mock_http_client, mock_github_copilot_response):
        """Test collection of free tier Copilot quotas."""
        collector = GitHubCollector()

        token_response = MagicMock(spec=httpx.Response)
        token_response.status_code = 200
        token_response.json.return_value = mock_github_copilot_response

        user_response = MagicMock(spec=httpx.Response)
        user_response.status_code = 200
        user_response.json.return_value = {"quota_snapshots": []}

        mock_http_client.get.side_effect = [token_response, user_response]

        with patch(
            "app.services.credential_provider.CredentialProvider.get_github_token",
            return_value="github_token",
        ):
            result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert any("Copilot" in str(card.get("service_name", "")) for card in result)

    @pytest.mark.asyncio
    async def test_collect_missing_token(self, mock_http_client):
        """Test that missing GitHub token returns an error card."""
        collector = GitHubCollector()

        with (
            patch(
                "app.services.credential_provider.CredentialProvider.get_github_data",
                return_value={},
            ),
            patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        ):
            result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert "Login required" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_collect_429_backoff_does_not_crash(self, mock_http_client):
        """Test that GitHub 429 responses create backoff instead of crashing."""
        collector = GitHubCollector(account_id="acc_a")

        rate_limited = MagicMock(spec=httpx.Response)
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "60"}

        mock_http_client.request.return_value = rate_limited

        with patch.object(
            collector, "_get_token", new_callable=AsyncMock, return_value="github_token"
        ):
            result = await collector._strategy_api(mock_http_client)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert result[0]["error_type"] == "rate_limited"
        assert collector._last_429_backoff_until is not None

    @pytest.mark.asyncio
    async def test_github_token_lookup_uses_account_scope(self, mock_http_client):
        """Test that dynamic GitHub collectors only read their own cached token."""
        collector = GitHubCollector(account_id="acc_a")

        with (
            patch(
                "app.services.collectors.github.credential_provider.get_github_data",
                return_value={},
            ),
            patch(
                "app.services.collectors.github.token_cache.get_token",
                new_callable=AsyncMock,
                return_value="scoped_token",
            ) as mock_get_token,
            patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        ):
            token = await collector._get_token()

        assert token == "scoped_token"
        mock_get_token.assert_awaited_once_with("github", "api_key", account_id="acc_a")

    @pytest.mark.asyncio
    async def test_github_default_token_lookup_does_not_use_sidecar_cache(self, mock_http_client):
        """Test that the default GitHub collector does not steal a dynamic account token."""
        collector = GitHubCollector()

        with (
            patch(
                "app.services.collectors.github.credential_provider.get_github_data",
                return_value={},
            ),
            patch(
                "app.services.collectors.github.token_cache.get_token",
                new_callable=AsyncMock,
            ) as mock_get_token,
            patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        ):
            token = await collector._get_token()

        assert token is None
        mock_get_token.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Caching removed by T2")
    async def test_collect_api_error_caching(self, mock_http_client):
        """Test that API results are cached to avoid hammering the API (same instance)."""
        collector = GitHubCollector()

        # Verify initial state - no cache
        assert collector._cached_results is None
        assert collector._last_fetch is None

        # Mock API error response (500 error)
        error_response = MagicMock(spec=httpx.Response)
        error_response.status_code = 500
        error_response.headers = {}

        # Use AsyncMock to track calls
        mock_request = AsyncMock(return_value=error_response)
        mock_http_client.request = mock_request

        with patch(
            "app.services.credential_provider.CredentialProvider.get_github_token",
            return_value="github_token",
        ):
            # First call - API fails
            result1 = await collector.collect(mock_http_client)
            first_call_count = mock_request.call_count

            # Verify cache was populated (any result)
            assert collector._cached_results is not None
            assert collector._last_fetch is not None

            # Second call with SAME collector instance - should use cache
            result2 = await collector.collect(mock_http_client)

            # API should not be called again (result was cached)
            assert mock_request.call_count == first_call_count

            # Results should be the same (from cache, ignoring timestamp)
            assert len(result1) == len(result2)
            for r1, r2 in zip(result1, result2):
                r1_copy = r1.copy()
                r2_copy = r2.copy()
                r1_copy.pop("updated_at", None)
                r2_copy.pop("updated_at", None)
                assert r1_copy == r2_copy


class TestChatGPTCollector:
    """Test suite for ChatGPT collector."""

    @pytest.mark.asyncio
    async def test_collect_api_success(self, mock_http_client, mock_chatgpt_usage_response):
        """Test successful ChatGPT API collection."""
        collector = ChatGPTCollector()

        # Mock Usage Info (Unified)
        usage_data = mock_chatgpt_usage_response.copy()
        usage_data["plan_type"] = "plus"

        usage_response = MagicMock(spec=httpx.Response)
        usage_response.status_code = 200
        usage_response.headers = {}
        usage_response.json.return_value = usage_data

        # Only one call needed now
        mock_http_client.request.side_effect = [usage_response]

        with patch(
            "app.services.credential_provider.CredentialProvider.get_chatgpt_data",
            return_value={"access_token": "test_token"},
        ):
            result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) == 1
        assert "Codex" in str(result[0].get("service_name", ""))
        assert "PLUS" in str(result[0].get("detail", ""))
        assert "%" in str(result[0].get("remaining", ""))

    @pytest.mark.asyncio
    async def test_collect_fallback_to_local_logs(self, mock_http_client):
        """Test fallback to local logs when API fails."""
        collector = ChatGPTCollector()

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 500
        mock_http_client.get.return_value = mock_response

        with patch("app.services.collectors.chatgpt_auth.settings") as mock_settings:
            mock_settings.CHATGPT_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_COLLECTOR_ENABLED = True

            with patch("builtins.open", side_effect=FileNotFoundError):
                result = await collector.collect(mock_http_client)

        # Should return error card if both API and logs fail
        assert isinstance(result, list)

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Caching removed by T2")
    async def test_collect_api_error_caching(self, mock_http_client):
        """Test that API results are cached to avoid hammering the API (same instance)."""

        collector = ChatGPTCollector()

        # Verify initial state - no cache
        assert collector._cached_api_results is None
        assert collector._last_api_fetch is None

        # Mock API error response (429 rate limit)
        error_response = MagicMock(spec=httpx.Response)
        error_response.status_code = 429
        mock_http_client.get.return_value = error_response

        with patch("app.services.collectors.chatgpt_auth.settings") as mock_settings:
            mock_settings.CHATGPT_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_COLLECTOR_ENABLED = True

            with patch(
                "app.services.collectors.chatgpt.ChatGPTCollector._collect_via_cli_rpc",
                return_value=[],
            ):
                with patch("builtins.open", side_effect=FileNotFoundError):
                    with patch(
                        "app.services.credential_provider.CredentialProvider.get_chatgpt_data",
                        return_value={"access_token": "test_token"},
                    ):
                        # First call - API fails, no logs
                        result1 = await collector.collect(mock_http_client)
                        first_call_count = mock_http_client.get.call_count

                        # Verify cache was populated (any result)
                        assert collector._cached_api_results is not None
                        assert collector._last_api_fetch is not None

                        # Second call with SAME collector instance - should use cache
                        result2 = await collector.collect(mock_http_client)

                        # API should not be called again (result was cached)
                        assert mock_http_client.get.call_count == first_call_count

                        # Both results should be error cards (may have slightly different messages)
                        assert any(r.get("remaining") == "ERR" for r in result1)
                        assert any(r.get("remaining") == "ERR" for r in result2)


class TestAntigravityCollector:
    """Test suite for Antigravity IDE collector."""

    @pytest.mark.asyncio
    async def test_collect_file_success(self, mock_http_client):
        """Test successful collection from Antigravity quota file."""
        collector = AntigravityCollector()

        quota_data = {
            "models": {
                "claude-3-opus": {"remaining_percent": 65.5, "resets_at": 1744876800},
                "claude-3-sonnet": {"remaining_percent": 72.3, "resets_at": 1744876800},
            }
        }

        with patch("builtins.open", mock_open(read_data=json.dumps(quota_data))):
            with patch("app.services.collectors.antigravity.settings") as mock_settings:
                mock_settings.ANTIGRAVITY_QUOTA_PATH = "/fake/quota.json"
                result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) == 2
        assert all("AG:" not in card.get("service_name", "") for card in result)
        assert all(card.get("provider_id") == "antigravity" for card in result)
        assert all(card.get("model_id") is not None for card in result)
        assert all(
            card.get("used_value") == pytest.approx(100.0 - card_rem, abs=0.01)
            for card in result
            for card_rem in [float(card["remaining"].rstrip("%"))]
        )

    @pytest.mark.asyncio
    async def test_collect_missing_file(self, mock_http_client):
        """Test graceful handling when quota file missing."""
        collector = AntigravityCollector()

        with patch("builtins.open", side_effect=FileNotFoundError):
            with patch("app.services.collectors.antigravity.settings") as mock_settings:
                mock_settings.ANTIGRAVITY_QUOTA_PATH = "/fake/missing.json"
                result = await collector.collect(mock_http_client)

        # Should return empty list
        assert result == []

    @pytest.mark.asyncio
    async def test_collect_lsp_with_credits(self, mock_http_client):
        """Test Antigravity collection from LSP including AI credits."""
        collector = AntigravityCollector()

        mock_response_data = {
            "userStatus": {
                "email": "test@example.com",
                "planStatus": {"planInfo": {"planName": "Pro"}},
                "cascadeModelConfigData": {
                    "clientModelConfigs": [
                        {
                            "label": "claude-3-opus",
                            "quotaInfo": {"remainingFraction": 0.5, "resetTime": 1744876800},
                        }
                    ]
                },
                "userTier": {
                    "name": "Pro",
                    "availableCredits": [{"creditType": "GOOGLE_ONE_AI", "creditAmount": "854"}],
                },
            }
        }

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_response_data
        mock_http_client.post.return_value = mock_resp

        # Mock LSP detection to return a PID
        with patch.object(collector, "_detect_lsp_proc_info", return_value={9999: ["token"]}):
            with patch.object(collector, "_find_listening_ports", return_value=[5000]):
                with patch("app.services.collectors.antigravity.settings") as mock_settings:
                    mock_settings.LOCAL_COLLECTOR_ENABLED = True
                    result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) == 2, (
            f"Expected 2 cards, got {len(result)}: {[c['service_name'] for c in result]}"
        )

        # Check quota card
        quota_cards = [c for c in result if "claude" in c["service_name"].lower()]
        assert len(quota_cards) == 1, (
            f"Quota card for 'claude' not found in {[c['service_name'] for c in result]}"
        )
        assert quota_cards[0]["remaining"] == "50.0%"

        # Check credits card
        credit_cards = [c for c in result if "credits" in c["service_name"].lower()]
        assert len(credit_cards) == 1, "Credits card not found"
        assert credit_cards[0]["remaining"] == "854"
        assert credit_cards[0]["service_name"] == "Google AI Credits"
        assert credit_cards[0]["icon"] == "💰"
        assert credit_cards[0]["unit"] == "credits"
        assert credit_cards[0]["provider_id"] == "antigravity"
        assert credit_cards[0]["account_label"] == "test@example.com"
        assert credit_cards[0]["used_value"] is None
        assert credit_cards[0]["limit_value"] is None

        # Check quota card enriched fields
        assert quota_cards[0]["provider_id"] == "antigravity"
        assert quota_cards[0]["account_label"] == "test@example.com"
        assert quota_cards[0]["used_value"] == pytest.approx(50.0, abs=0.01)
        assert quota_cards[0]["limit_value"] == 100.0
        assert quota_cards[0]["unit_type"] == "percent"
        assert quota_cards[0]["window_type"] == "session"
        assert quota_cards[0]["reset_at"] is not None
        assert quota_cards[0]["reset"] == "Expired"  # resetTime: 1744876800 is in the past

    def test_format_reset_with_future_timestamp(self):
        """_format_reset returns display string and ISO string for future timestamps."""
        import time

        from app.services.collectors.antigravity import _format_reset

        future_ts = int(time.time()) + 7320  # 2 hours 2 minutes from now
        display, reset_at = _format_reset(future_ts)

        assert "2h" in display
        assert reset_at is not None
        assert "T" in reset_at  # ISO 8601 contains T separator

    def test_format_reset_with_none(self):
        """_format_reset returns Dynamic and None for missing timestamps."""
        from app.services.collectors.antigravity import _format_reset

        display, reset_at = _format_reset(None)
        assert display == "Dynamic"
        assert reset_at is None

    def test_format_reset_with_near_future_timestamp(self):
        """_format_reset returns '< 1m' for timestamps less than 60 seconds away."""
        import time

        from app.services.collectors.antigravity import _format_reset

        near_future_ts = int(time.time()) + 30
        display, reset_at = _format_reset(near_future_ts)
        assert display == "< 1m"
        assert reset_at is not None

    def test_format_reset_with_past_timestamp(self):
        """_format_reset returns 'Expired' for timestamps in the past."""
        import time

        from app.services.collectors.antigravity import _format_reset

        past_ts = int(time.time()) - 3600
        display, reset_at = _format_reset(past_ts)
        assert display == "Expired"
        assert reset_at is not None

    @pytest.mark.asyncio
    async def test_local_file_includes_reset_at(self, mock_http_client):
        """Local file cards include reset_at when resets_at is present."""
        import time
        from unittest.mock import mock_open, patch

        collector = AntigravityCollector()

        quota_data = {
            "models": {
                "claude-sonnet-4": {
                    "remaining_percent": 75.5,
                    "resets_at": int(time.time()) + 3600,
                }
            }
        }

        with patch("builtins.open", mock_open(read_data=json.dumps(quota_data))):
            with patch("app.services.collectors.antigravity.settings") as mock_settings:
                mock_settings.ANTIGRAVITY_QUOTA_PATH = "/fake/quota.json"
                mock_settings.LOCAL_COLLECTOR_ENABLED = True
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        card = result[0]
        assert card["service_name"] == "claude-sonnet-4"
        assert card["provider_id"] == "antigravity"
        assert card["model_id"] == "claude-sonnet-4"
        assert card["reset_at"] is not None
        assert card["used_value"] == pytest.approx(24.5, abs=0.1)
        assert (
            card["account_label"] == "Default"
        )  # no email from file; base collector fills in "Default"


class TestOpenCodeCollector:
    """Test suite for OpenCode collector."""

    @pytest.mark.asyncio
    async def test_collect_returns_list(self, mock_http_client):
        """Test OpenCode collector returns a list (may be empty if no data sources available)."""
        collector = OpenCodeCollector()

        # Mock all external dependencies to simulate no data available
        with (
            patch(
                "app.services.collectors.opencode.get_opencode_session_cookie",
                return_value=None,
            ),
            patch("app.services.collectors.opencode.external_metric_service") as mock_external,
        ):
            # Use AsyncMock for the awaited call
            mock_external.get_opencode_aggregated = AsyncMock(return_value=[])

            # Mock local DB doesn't exist
            with patch(
                "app.services.collectors.opencode.os.path.exists",
                return_value=False,
            ):
                result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        # When no data sources are available, should return empty list
        assert result == []


class TestZaiCollector:
    """Test suite for zAI (consolidated) collector."""

    @pytest.mark.asyncio
    async def test_collect_quota_success_token_limit(self, mock_http_client):
        """Test successful zAI plan collection with token limit."""
        collector = ZaiCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "code": 200,
            "success": True,
            "data": {
                "planName": "Basic Plan",
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "unit": 3,
                        "number": 168,
                        "usage": 1000000,
                        "currentValue": 450000,
                        "remaining": 550000,
                        "percentage": 45,
                        "nextResetTime": 1775570736000,
                    }
                ],
            },
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai_valid_key"
            result = await collector.collect(mock_http_client)

        assert len(result) >= 1
        token_card = next((c for c in result if "Tokens" in c.get("service_name", "")), None)
        assert token_card is not None
        assert "550,000" in token_card["remaining"]
        assert token_card["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_quota_success_both_limits(self, mock_http_client):
        """Test successful zAI plan collection with both token and time limits."""
        collector = ZaiCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "code": 200,
            "success": True,
            "data": {
                "planName": "Pro Plan",
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "unit": 3,
                        "number": 168,
                        "usage": 1000000,
                        "currentValue": 200000,
                        "remaining": 800000,
                        "percentage": 20,
                        "nextResetTime": 1775570736000,
                    },
                    {
                        "type": "TIME_LIMIT",
                        "unit": 5,
                        "number": 43200,
                        "usage": 3600,
                        "currentValue": 900,
                        "remaining": 2700,
                        "percentage": 25,
                        "nextResetTime": 1775570736000,
                    },
                ],
            },
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai_valid_key"
            result = await collector.collect(mock_http_client)

        assert len(result) >= 2
        assert any("Tokens" in c.get("service_name", "") for c in result)
        assert any("Time" in c.get("service_name", "") for c in result)

    @pytest.mark.asyncio
    async def test_collect_no_plan_returns_info_card(self, mock_http_client):
        """Test zAI returns info card when no plan/limits available."""
        collector = ZaiCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "code": 200,
            "success": True,
            "data": {"limits": []},
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai_valid_key"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "No active plan"
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_invalid_key_returns_info_card(self, mock_http_client):
        """Test zAI returns info card when key is placeholder."""
        collector = ZaiCollector()

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai"
            with patch(
                "app.services.collectors.zai.credential_provider.get_provider_api_key",
                return_value=None,
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert "zAI" in result[0]["service_name"]
        assert result[0]["remaining"] == "No API key configured"
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_no_key_returns_info_card(self, mock_http_client):
        """Test zAI returns info card when no API key."""
        collector = ZaiCollector()

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = ""
            with patch(
                "app.services.collectors.zai.credential_provider.get_provider_api_key",
                return_value=None,
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "No API key configured"
        assert result[0]["health"] == "good"


class TestKimiApiCollector:
    """Test suite for Kimi API (Balance) collector."""

    @pytest.mark.asyncio
    async def test_collect_success(self, mock_http_client, mock_kimi_response):
        """Test successful Kimi API balance collection."""
        collector = KimiApiCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = mock_kimi_response

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.kimi_api.settings") as mock_settings:
            mock_settings.KIMI_API_KEY = "kimi_valid_key_long"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service_name"] == "Kimi API"
        assert "$8.75" in result[0]["remaining"]
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_invalid_key(self, mock_http_client):
        """Test Kimi API collection with short/invalid key."""
        collector = KimiApiCollector()

        with patch("app.services.collectors.kimi_api.settings") as mock_settings:
            mock_settings.KIMI_API_KEY = "short"
            with patch(
                "app.services.collectors.kimi_api.credential_provider.get_provider_api_key",
                return_value=None,
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert "Kimi API" in result[0]["service_name"]
        assert result[0]["remaining"] == "ERR"
        assert "Missing/Invalid Key" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_collect_unauthorized(self, mock_http_client):
        """Test Kimi API collection with 401 Unauthorized."""
        collector = KimiApiCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 401

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.kimi_api.settings") as mock_settings:
            mock_settings.KIMI_API_KEY = "invalid_key_long"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert "Unauthorized" in result[0]["detail"]


class TestKimiK2Collector:
    """Test suite for Kimi K2 credits collector."""

    @pytest.mark.asyncio
    async def test_collect_success(self, mock_http_client):
        """Test successful Kimi K2 credits collection."""
        collector = KimiK2Collector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "credits_remaining": 500.00,
            "credits_consumed": 150.00,
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.kimi_k2.settings") as mock_settings:
            mock_settings.KIMI_K2_API_KEY = "kimi_k2_valid_key_long"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service_name"] == "Kimi K2"
        assert result[0]["remaining"] == "500.00"
        assert "credits" in result[0]["unit"]
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_low_credits_warning(self, mock_http_client):
        """Test Kimi K2 shows warning when credits are low."""
        collector = KimiK2Collector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "credits_remaining": 50.00,
            "credits_consumed": 450.00,
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.kimi_k2.settings") as mock_settings:
            mock_settings.KIMI_K2_API_KEY = "kimi_k2_valid_key_long"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "50.00"
        assert result[0]["health"] == "warning"

    @pytest.mark.asyncio
    async def test_collect_zero_credits_critical(self, mock_http_client):
        """Test Kimi K2 shows critical when credits are zero."""
        collector = KimiK2Collector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "credits_remaining": 0,
            "credits_consumed": 600.00,
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.kimi_k2.settings") as mock_settings:
            mock_settings.KIMI_K2_API_KEY = "kimi_k2_valid_key_long"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "0.00"
        assert result[0]["health"] == "critical"

    @pytest.mark.asyncio
    async def test_collect_invalid_key(self, mock_http_client):
        """Test Kimi K2 collection with short/invalid key."""
        collector = KimiK2Collector()

        with patch("app.services.collectors.kimi_k2.settings") as mock_settings:
            mock_settings.KIMI_K2_API_KEY = "short"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert "Kimi K2" in result[0]["service_name"]
        assert result[0]["remaining"] == "ERR"
        assert "Missing/Invalid Key" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_collect_unauthorized(self, mock_http_client):
        """Test Kimi K2 collection with 401 Unauthorized."""
        collector = KimiK2Collector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 401

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.kimi_k2.settings") as mock_settings:
            mock_settings.KIMI_K2_API_KEY = "invalid_key_long"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert "Unauthorized" in result[0]["detail"]


class TestKimiCodingCollector:
    """Test suite for Kimi Coding (IDE) collector."""

    @pytest.mark.asyncio
    async def test_collect_success_with_env_var(self, mock_http_client):
        """Test successful Kimi Coding collection with env var auth."""
        collector = KimiCodingCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "usages": [
                {
                    "scope": "FEATURE_CODING",
                    "detail": {
                        "limit": "2048",
                        "used": "214",
                        "remaining": "1834",
                        "resetTime": "2026-01-09T15:23:13Z",
                    },
                    "limits": [
                        {
                            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                            "detail": {
                                "limit": "200",
                                "used": "139",
                                "remaining": "61",
                                "resetTime": "2026-01-06T13:33:02Z",
                            },
                        }
                    ],
                }
            ]
        }

        mock_http_client.post.return_value = response

        with patch("app.services.collectors.kimi_coding.settings") as mock_settings:
            mock_settings.KIMI_AUTH_TOKEN = "jwt_token_here"
            result = await collector.collect(mock_http_client)

        assert len(result) == 2
        assert any("Weekly" in card["service_name"] for card in result)
        assert any("5h" in card["service_name"] for card in result)
        assert any("Moderato" in card["detail"] for card in result)

    @pytest.mark.asyncio
    async def test_collect_empty_usage(self, mock_http_client):
        """Test Kimi Coding collection with empty usage response."""
        collector = KimiCodingCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {}

        mock_http_client.post.return_value = response

        with patch("app.services.collectors.kimi_coding.settings") as mock_settings:
            mock_settings.KIMI_AUTH_TOKEN = "jwt_token_here"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert "No active plan" in result[0]["detail"]
        assert result[0]["remaining"] == "No active plan"

    @pytest.mark.asyncio
    async def test_collect_no_auth(self, mock_http_client):
        """Test Kimi Coding collection without auth."""
        collector = KimiCodingCollector()

        with patch("app.services.collectors.kimi_coding.settings") as mock_settings:
            mock_settings.KIMI_AUTH_TOKEN = ""
            with patch("app.services.collectors.kimi_coding.get_kimi_auth_cookie") as mock_cookie:
                mock_cookie.return_value = None
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"

    @pytest.mark.asyncio
    async def test_collect_api_error(self, mock_http_client):
        """Test Kimi Coding collection with API error."""
        collector = KimiCodingCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 401

        mock_http_client.post.return_value = response

        with patch("app.services.collectors.kimi_coding.settings") as mock_settings:
            mock_settings.KIMI_AUTH_TOKEN = "invalid_token"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"


class TestOpenRouterCollector:
    """Test suite for OpenRouter collector."""

    @pytest.mark.asyncio
    async def test_collect_success(self, mock_http_client):
        """Test successful OpenRouter credits-only collection."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            key_resp = MagicMock(spec=httpx.Response)
            key_resp.status_code = 200
            key_resp.json.return_value = {"data": {"limit": None, "usage": 0.0}}

            with (
                patch(
                    "app.services.collectors.openrouter.http_request_with_retry",
                    new_callable=AsyncMock,
                    return_value=credits_resp,
                ),
                patch.object(
                    collector,
                    "_key_endpoint_request",
                    new_callable=AsyncMock,
                    return_value=key_resp,
                ),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service_name"] == "OpenRouter Credits"
        assert "$7.50" in result[0]["remaining"]
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_success_with_key_limit(self, mock_http_client):
        """Test collection returns two cards when key endpoint has a spending limit."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            key_resp = MagicMock(spec=httpx.Response)
            key_resp.status_code = 200
            key_resp.json.return_value = {"data": {"limit": 20.0, "usage": 0.5}}

            with (
                patch(
                    "app.services.collectors.openrouter.http_request_with_retry",
                    new_callable=AsyncMock,
                    return_value=credits_resp,
                ),
                patch.object(
                    collector,
                    "_key_endpoint_request",
                    new_callable=AsyncMock,
                    return_value=key_resp,
                ),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 2
        services = {c["service_name"]: c for c in result}
        assert "OpenRouter Credits" in services
        assert "OpenRouter Key Limit" in services
        key_card = services["OpenRouter Key Limit"]
        assert "$19.50" in key_card["remaining"]
        assert key_card["health"] == "good"
        assert key_card["limit_value"] == 20.0
        assert key_card["used_value"] == 0.5

    @pytest.mark.asyncio
    async def test_collect_key_endpoint_failure_graceful(self, mock_http_client):
        """Test that key endpoint failure still returns the credits card."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            key_resp = MagicMock(spec=httpx.Response)
            key_resp.status_code = 500
            key_resp.text = "Internal Server Error"

            with (
                patch(
                    "app.services.collectors.openrouter.http_request_with_retry",
                    new_callable=AsyncMock,
                    return_value=credits_resp,
                ),
                patch.object(
                    collector,
                    "_key_endpoint_request",
                    new_callable=AsyncMock,
                    return_value=key_resp,
                ),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service_name"] == "OpenRouter Credits"

    @pytest.mark.asyncio
    async def test_collect_key_timeout_still_returns_credits(self, mock_http_client):
        """Test that key endpoint timeout still returns the credits card."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            with (
                patch(
                    "app.services.collectors.openrouter.http_request_with_retry",
                    new_callable=AsyncMock,
                    return_value=credits_resp,
                ),
                patch.object(
                    collector,
                    "_key_endpoint_request",
                    new_callable=AsyncMock,
                    side_effect=httpx.TimeoutException("timeout"),
                ),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service_name"] == "OpenRouter Credits"

    @pytest.mark.asyncio
    async def test_collect_key_no_limit_configured(self, mock_http_client):
        """Test that key endpoint with no limit configured only returns credits card."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            key_resp = MagicMock(spec=httpx.Response)
            key_resp.status_code = 200
            key_resp.json.return_value = {"data": {"limit": None, "usage": 0.0}}

            with (
                patch(
                    "app.services.collectors.openrouter.http_request_with_retry",
                    new_callable=AsyncMock,
                    return_value=credits_resp,
                ),
                patch.object(
                    collector,
                    "_key_endpoint_request",
                    new_callable=AsyncMock,
                    return_value=key_resp,
                ),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service_name"] == "OpenRouter Credits"

    @pytest.mark.asyncio
    async def test_headers_include_referer_and_title(self, mock_http_client):
        """Test that HTTP-Referer and X-Title headers are sent when configured."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = "https://example.com"
            mock_settings.OPENROUTER_X_TITLE = "MyApp"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            key_resp = MagicMock(spec=httpx.Response)
            key_resp.status_code = 500
            key_resp.text = "error"

            mock_http_client.get.side_effect = [credits_resp, key_resp]
            await collector.collect(mock_http_client)

        calls = mock_http_client.get.call_args_list
        for call in calls:
            headers = call.kwargs.get("headers", {})
            assert headers.get("HTTP-Referer") == "https://example.com"
            assert headers.get("X-Title") == "MyApp"

    @pytest.mark.asyncio
    async def test_collect_api_error(self, mock_http_client):
        """Test OpenRouter collection with credits API error."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 500
            response.text = "Internal Server Error"
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert "API connection failed" in result[0]["detail"]


class TestMiniMaxCollector:
    """Test suite for MiniMax collector."""

    @pytest.mark.asyncio
    async def test_collect_success(self, mock_http_client):
        """Test successful MiniMax API collection."""
        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "mm_valid_key"
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = {
                "model_remains": [{"model_name": "minimax-text-01", "remains": 500}]
            }

            mock_http_client.get.return_value = response
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert "MiniMax" in result[0]["service_name"]
        assert "500" in result[0]["remaining"]
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_api_error(self, mock_http_client):
        """Test MiniMax collection with API error."""
        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "mm_valid_key"
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 403
            response.text = "Forbidden"
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert "API connection failed" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_collect_no_active_plan(self, mock_http_client):
        """Test MiniMax returns 'No active plan' when API returns empty model_remains."""
        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "mm_valid_key"
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = {"model_remains": []}

            mock_http_client.get.return_value = response
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert "No active plan" in result[0]["detail"]
        assert result[0]["remaining"] == "No active plan"


class TestHttpTimeouts:
    """I2: All collector HTTP calls must pass an explicit timeout."""

    @pytest.mark.asyncio
    async def test_github_collector_passes_timeout(self, mock_http_client):
        """GitHub collector must pass timeout= on all HTTP calls."""
        from app.services.collectors.github import GitHubCollector

        collector = GitHubCollector()

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"assignee": {"chat_enabled": True}}
        mock_http_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.collectors.github.credential_provider") as mock_cp:
            mock_cp.get_github_token.return_value = "ghp_test123"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert "timeout" in call.kwargs, f"GitHub HTTP call #{i} missing timeout=: {call}"

    @pytest.mark.asyncio
    async def test_zai_collector_passes_timeout(self, mock_http_client):
        """ZAI collector must pass timeout= on its HTTP call."""
        from app.services.collectors.zai import ZaiCollector

        collector = ZaiCollector()

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"available_balance": "10.0"}}
        mock_http_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "test_key"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert "timeout" in call.kwargs, f"ZAI HTTP call #{i} missing timeout=: {call}"

    @pytest.mark.asyncio
    async def test_kimi_coding_collector_passes_timeout(self, mock_http_client):
        """Kimi Coding collector must pass timeout= on its HTTP call."""
        from app.services.collectors.kimi_coding import KimiCodingCollector

        collector = KimiCodingCollector()

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_http_client.post = AsyncMock(return_value=mock_resp)

        with patch("app.services.collectors.kimi_coding.settings") as mock_settings:
            mock_settings.KIMI_AUTH_TOKEN = "test_kimi_key"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.post.call_args_list):
            assert "timeout" in call.kwargs, f"Kimi Coding HTTP call #{i} missing timeout=: {call}"

    @pytest.mark.asyncio
    async def test_openrouter_collector_passes_timeout(self, mock_http_client):
        """OpenRouter collector must pass timeout= on its HTTP call."""
        collector = OpenRouterCollector()
        credits_resp = MagicMock(spec=httpx.Response)
        credits_resp.status_code = 200
        credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

        key_resp = MagicMock(spec=httpx.Response)
        key_resp.status_code = 200
        key_resp.json.return_value = {"data": {"limit": None, "usage": 0.0}}

        mock_http_client.get = AsyncMock(side_effect=[credits_resp, key_resp])

        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "test_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert "timeout" in call.kwargs, f"OpenRouter HTTP call #{i} missing timeout=: {call}"

    @pytest.mark.asyncio
    async def test_minimax_collector_passes_timeout(self, mock_http_client):
        """MiniMax collector must pass timeout= on its HTTP call."""
        collector = MiniMaxCollector()
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"model_remains": []}
        mock_http_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "test_key"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert "timeout" in call.kwargs, f"MiniMax HTTP call #{i} missing timeout=: {call}"
