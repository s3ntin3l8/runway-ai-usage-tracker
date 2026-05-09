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
    @pytest.mark.skip(reason="browser-cookie / local fallback moved to sidecar")
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
        assert any(card.get("window_type") == "session" for card in result)
        # seven_day_sonnet must fold into weekly window_type with model_id="sonnet"
        sonnet_card = next(
            (
                c
                for c in result
                if c.get("window_type") == "weekly" and c.get("model_id") == "sonnet"
            ),
            None,
        )
        assert sonnet_card is not None, "Expected a Sonnet Weekly card from mock data"

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
    @pytest.mark.skip(reason="local strategy moved to sidecar")
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
    async def test_collect_oauth_429_returns_error_card(
        self, mock_sleep, mock_http_client, mock_anthropic_oauth_response
    ):
        """Test that 429 rate limit returns error card with rate_limited type."""
        collector = AnthropicCollector()

        # Mock 429 rate limit response with a Retry-After header
        mock_429_response = MagicMock(spec=httpx.Response)
        mock_429_response.status_code = 429
        mock_429_response.headers = {"Retry-After": "60"}  # 60 seconds

        mock_http_client.request.return_value = mock_429_response

        with (
            patch.object(collector, "_get_valid_token", return_value="test_token"),
        ):
            # Call the OAuth strategy directly to verify 429 handling
            result = await collector._get_claude_oauth(mock_http_client, "test_token")
            assert result[0].get("error_type") == "rate_limited"
            assert collector._last_retry_after == 300.0

            # Second direct call - still returns 429 (no proactive backoff)
            result2 = await collector._get_claude_oauth(mock_http_client, "test_token")
            assert result2[0].get("error_type") == "rate_limited"

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
            with patch.object(collector, "_get_valid_token", return_value="test_token"):
                result = await collector.collect(mock_http_client)

        # Should return 5 cards: 3 standard quota windows (Session, Weekly, Sonnet) + Balance + Extra Usage
        assert len(result) == 5

        variants = {c.get("variant"): c for c in result if c.get("variant")}
        assert "Current Balance" in variants
        assert "Extra Usage" in variants
        # Plus a Session window-percent card with variant=null
        assert any(c.get("window_type") == "session" and c.get("variant") is None for c in result)

        # Check Balance card
        bal_card = variants["Current Balance"]
        assert bal_card["remaining"] == "$15.75"
        assert bal_card["unit"] == "USD"
        assert bal_card["icon"] == "💰"

        # Check Extra Usage card
        extra_card = variants["Extra Usage"]
        assert extra_card["remaining"] == "$17.50"  # 20.00 - 2.50
        assert extra_card["unit"] == "limit"
        assert "Spent: $2.50" in extra_card["detail"]

    @pytest.mark.asyncio
    async def test_collect_oauth_success_after_429(
        self, mock_http_client, mock_anthropic_oauth_response
    ):
        """Test that a successful call works after a previous 429."""
        collector = AnthropicCollector()

        # Mock success response
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_anthropic_oauth_response
        mock_response.headers = {}
        mock_http_client.request.return_value = mock_response

        with patch.object(collector, "_get_valid_token", return_value="test_token"):
            result = await collector.collect(mock_http_client)

            # Should return successful results
            assert len(result) > 0
            assert result[0].get("error_type") is None

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
                    [{"service_name": "Claude", "remaining": "50%", "data_source": "api"}],
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
        assert any(card.get("data_source") == "api" for card in result)

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="browser-cookie / local fallback moved to sidecar")
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

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value="sk-ant-session123",
                ),
                patch.object(collector, "_get_valid_token", return_value="invalid_token"),
            ):
                result = await collector.collect(mock_http_client)

        # Should return Web API results — only 3 cards because seven_day_opus is missing from mock data
        assert isinstance(result, list)
        assert len(result) == 3
        window_types = [r.get("window_type") for r in result]
        assert "session" in window_types
        assert "weekly" in window_types
        assert any(card.get("data_source") == "web" for card in result)
        # Fixture uses snake_case resets_at — must NOT be dropped (regression for camelCase-only bug)
        assert all(card.get("reset_at") is not None for card in result), (
            "reset_at should be populated from snake_case resets_at in web API payload"
        )
        assert all(card.get("reset") not in ("—", None) for card in result), (
            "reset human-delta should be non-empty when reset_at is populated"
        )

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local strategy moved to sidecar")
    async def test_collect_enhanced_local_no_fallback(self, mock_http_client):
        """Enrichment does not act as fallback when OAuth and Web API fail."""
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

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value=None,
                ),
                patch.object(collector, "_get_valid_token", return_value="invalid_token"),
            ):
                # Mock local log data with all token types (correct JSONL field names)
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
                                    "cache_read_input_tokens": 2000,
                                    "cache_creation_input_tokens": 100,
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
                                "id": "msg_2",
                                "requestId": "req_2",
                                "usage": {
                                    "input_tokens": 500,
                                    "output_tokens": 200,
                                    "cache_read_input_tokens": 0,
                                    "cache_creation_input_tokens": 0,
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

        # Error card remains; enrichment does not promote fallback
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"

    @pytest.mark.skip(reason="local strategy moved to sidecar")
    def test_get_claude_local_enhanced_sync_dedup(self, tmp_path):
        """Test deduplication of streaming chunks in local logs."""
        collector = AnthropicCollector()

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
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
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
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            )
            + "\n",
        ]

        with (
            patch("app.services.collectors.anthropic.settings") as mock_settings,
            patch("builtins.open", mock_open(read_data="".join(log_data))),
            patch(
                "app.services.collectors.anthropic_local.glob.glob",
                return_value=["/fake/path/test.jsonl"],
            ),
            patch("os.path.isdir", return_value=True),
        ):
            mock_settings.CLAUDE_PRO_LIMIT = 2000000
            mock_settings.CLAUDE_FREE_LIMIT = 500000
            result = collector._get_claude_local_enhanced_sync()

        # Should deduplicate - only count once
        assert isinstance(result, list)
        assert len(result) == 2  # session + weekly
        by_wt = {r["window_type"]: r for r in result}
        sess = by_wt["session"]
        # Should only show 1500 tokens (not 3000 from duplicate)
        assert sess["msgs"] == 1
        assert sess["token_usage"]["input"] == 1000
        assert sess["token_usage"]["output"] == 500
        week = by_wt["weekly"]
        assert week["msgs"] == 1

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local strategy moved to sidecar")
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

        # Full identity - should only return email if present
        org_data = {
            "name": "Test Org",
            "membership": {"user": {"email": "user@example.com"}},
        }
        identity = collector._extract_identity_from_web(org_data)
        assert identity == "user@example.com"

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
    @pytest.mark.skip(reason="browser-cookie / local fallback moved to sidecar")
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
    @pytest.mark.skip(reason="browser-cookie / local fallback moved to sidecar")
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

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value="session_key",
                ),
                patch.object(collector, "_get_valid_token", return_value="invalid_token"),
            ):
                result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) == 3  # only 3 core windows in mock usage data
        assert any(card.get("data_source") == "web" for card in result)

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
        # Returns 3 items: all core keys (Session, Weekly, Sonnet) get default cards when null
        assert isinstance(result, list)
        assert len(result) == 3
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
    @pytest.mark.skip(reason="local strategy moved to sidecar")
    async def test_get_claude_local_enhanced_uses_to_thread(self):
        """C5: _get_claude_local_enhanced must delegate sync I/O to asyncio.to_thread."""

        collector = AnthropicCollector()

        with patch("app.services.collectors.anthropic_local.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value=None)
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
    @pytest.mark.skip(reason="local strategy moved to sidecar")
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

        with (
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch("shutil.which", return_value="/mock/bin/claude"),
        ):
            # Second call: claude -> returns output
            mock_exec.side_effect = [mock_cli]

            result = await collector._collect_via_cli_pty()

        assert len(result) == 2
        # Check Session card
        assert result[0]["service_name"] == "Claude"
        assert result[0].get("window_type") == "session"
        assert result[0]["used_value"] == 12.5
        assert result[0]["remaining"] == "87.5%"
        assert result[0]["data_source"] == "local"
        assert "[CLI PTY]" in result[0]["detail"]

        # Check Weekly card
        assert result[1].get("window_type") == "weekly"
        assert result[1]["used_value"] == 5.0
        assert result[1]["remaining"] == "95.0%"
        assert "4d" in result[1]["reset"]

    @pytest.mark.skip(reason="local strategy moved to sidecar")
    def test_get_claude_local_enhanced_sync_per_window(self, tmp_path):
        """Local log sync returns per-window enrichment dicts with correct bucketing."""
        now = datetime.now(UTC)
        ts_5h = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ts_7d = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        entry_session = json.dumps(
            {
                "type": "assistant",
                "timestamp": ts_5h,
                "sessionId": "sess-a",
                "message": {
                    "id": "msg-1",
                    "requestId": "req-1",
                    "model": "claude-opus-4-7",
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cache_read_input_tokens": 3000,
                        "cache_creation_input_tokens": 200,
                        "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
                    },
                },
            }
        )
        entry_weekly = json.dumps(
            {
                "type": "assistant",
                "timestamp": ts_7d,
                "sessionId": "sess-b",
                "message": {
                    "id": "msg-2",
                    "requestId": "req-2",
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 500,
                        "output_tokens": 200,
                        "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 100,
                    },
                },
            }
        )

        proj_dir = tmp_path / "projects" / "my-proj"
        proj_dir.mkdir(parents=True)
        (proj_dir / "session1.jsonl").write_text(entry_session + "\n")
        (proj_dir / "session2.jsonl").write_text(entry_weekly + "\n")

        collector = AnthropicCollector()

        with (
            patch.object(collector, "_get_config_dirs", return_value=[str(tmp_path / "projects")]),
            patch.object(collector, "_credentials_path", str(tmp_path / "no_creds.json")),
        ):
            result = collector._get_claude_local_enhanced_sync()

        # 4 dicts: session aggregate + weekly aggregate + sonnet weekly + opus weekly
        assert len(result) == 4
        by_key = {(r["window_type"], r.get("model_id")): r for r in result}

        # Session aggregate
        assert ("session", None) in by_key
        sess = by_key[("session", None)]
        assert "_enrichment_detail" in sess
        assert "token_usage" in sess
        assert "by_model" in sess
        assert "msgs" in sess
        assert "opus" in sess["_enrichment_detail"]
        assert sess["token_usage"]["input"] == 4200
        assert sess["token_usage"]["output"] == 500
        assert sess["msgs"] == 1
        assert "tokens" in sess["by_model"]["opus"]
        assert sess["by_model"]["opus"]["tokens"]["total"] == 4700

        # Weekly aggregate
        assert ("weekly", None) in by_key
        week = by_key[("weekly", None)]
        assert "_enrichment_detail" in week
        assert "opus" in week["_enrichment_detail"]
        assert "sonnet" in week["_enrichment_detail"]
        assert week["token_usage"]["input"] == 5800
        assert week["msgs"] == 2
        assert "tokens" in week["by_model"]["opus"]
        assert "tokens" in week["by_model"]["sonnet"]

        # Weekly Sonnet-specific
        assert ("weekly", "sonnet") in by_key
        week_sonnet = by_key[("weekly", "sonnet")]
        assert week_sonnet["token_usage"]["input"] == 1600
        assert week_sonnet["msgs"] == 1
        assert "sonnet" in week_sonnet["_enrichment_detail"]
        assert "opus" not in week_sonnet["_enrichment_detail"]
        assert "tokens" in week_sonnet["by_model"]["sonnet"]
        assert week_sonnet["by_model"]["sonnet"]["tokens"]["total"] == 1800

        # Weekly Opus-specific
        assert ("weekly", "opus") in by_key
        week_opus = by_key[("weekly", "opus")]
        assert week_opus["token_usage"]["input"] == 4200
        assert week_opus["msgs"] == 1
        assert "opus" in week_opus["_enrichment_detail"]
        assert "sonnet" not in week_opus["_enrichment_detail"]
        assert "tokens" in week_opus["by_model"]["opus"]
        assert week_opus["by_model"]["opus"]["tokens"]["total"] == 4700

    @pytest.mark.skip(reason="local strategy moved to sidecar")
    def test_enrichment_isolation_composite_keys(self, tmp_path):
        """Enrichment should isolate usage by (window_type, model_id) using composite reset keys."""
        now = datetime.now(UTC)

        # Message timestamps
        ts_sonnet = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # 3h ago
        ts_haiku = (now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # 10h ago

        # Scenario:
        # - Aggregate weekly reset was 12h ago.
        # - Sonnet specific weekly reset was 2h ago.
        # Result:
        # - Sonnet card should be EMPTY (message is 3h ago, reset was 2h ago).
        # - Aggregate card should include BOTH (resets 12h ago).

        sonnet_msg = json.dumps(
            {
                "type": "assistant",
                "timestamp": ts_sonnet,
                "sessionId": "s1",
                "message": {
                    "id": "m1",
                    "model": "claude-3-5-sonnet-latest",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            }
        )
        haiku_msg = json.dumps(
            {
                "type": "assistant",
                "timestamp": ts_haiku,
                "sessionId": "s2",
                "message": {
                    "id": "m2",
                    "model": "claude-3-haiku-20240307",
                    "usage": {"input_tokens": 200, "output_tokens": 100},
                },
            }
        )

        proj_dir = tmp_path / "projects" / "test-proj"
        proj_dir.mkdir(parents=True)
        (proj_dir / "logs.jsonl").write_text(sonnet_msg + "\n" + haiku_msg + "\n")

        collector = AnthropicCollector()

        # Configure reset times (future reset at now + duration)
        # Window is 7 days. If reset is in 166h, window started now - 2h.
        sonnet_reset = now + timedelta(hours=166)  # Window started 2h ago
        aggregate_reset = now + timedelta(hours=156)  # Window started 12h ago

        collector._window_resets = {
            ("weekly", "sonnet"): sonnet_reset,
            ("weekly", None): aggregate_reset,
        }

        with (
            patch.object(collector, "_get_config_dirs", return_value=[str(tmp_path / "projects")]),
            patch.object(collector, "_credentials_path", str(tmp_path / "no_creds.json")),
        ):
            result = collector._get_claude_local_enhanced_sync()

        by_key = {(r["window_type"], r.get("model_id")): r for r in result}

        # 1. Aggregate Weekly should have both (3h ago and 10h ago are > 12h ago)
        assert ("weekly", None) in by_key
        agg = by_key[("weekly", None)]
        assert agg["token_usage"]["input"] == 300  # 100 + 200

        # 2. Sonnet Weekly should be missing (3h ago is < 2h before reset)
        assert ("weekly", "sonnet") not in by_key

        # 3. Haiku Weekly should have its message (uses aggregate reset since no specific one set)
        assert ("weekly", "haiku") in by_key
        haiku = by_key[("weekly", "haiku")]
        assert haiku["token_usage"]["input"] == 200

    @pytest.mark.skip(reason="local strategy moved to sidecar")
    def test_enrichment_respects_primary_reset_at(self, tmp_path):
        """Enrichment should only count tokens since the primary card's reset_at."""
        import json
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        # Two messages: one before reset, one after
        ts_before = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ts_after = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        reset_at = (now - timedelta(days=1)).isoformat()

        entries = [
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": ts_before,
                    "sessionId": "sess-old",
                    "message": {
                        "id": "msg-old",
                        "requestId": "req-old",
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 5000, "output_tokens": 2000},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": ts_after,
                    "sessionId": "sess-new",
                    "message": {
                        "id": "msg-new",
                        "requestId": "req-new",
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                    },
                }
            ),
        ]

        proj_dir = tmp_path / "projects" / "my-proj"
        proj_dir.mkdir(parents=True)
        (proj_dir / "session.jsonl").write_text("\n".join(entries) + "\n")

        collector = AnthropicCollector()
        # Simulate primary metadata discovery using composite keys (window_type, model_id)
        collector._window_resets = {("weekly", None): datetime.fromisoformat(reset_at)}

        with (
            patch.object(collector, "_get_config_dirs", return_value=[str(tmp_path / "projects")]),
            patch.object(collector, "_credentials_path", str(tmp_path / "no_creds.json")),
        ):
            result = collector._get_claude_local_enhanced_sync()

        weekly = next(
            (r for r in result if r["window_type"] == "weekly" and r.get("model_id") is None), None
        )
        assert weekly is not None
        # Should only count the post-reset message (100 input + 50 output)
        assert weekly["token_usage"]["input"] == 100
        assert weekly["token_usage"]["output"] == 50
        assert weekly["msgs"] == 1

    def test_enrich_results_matches_by_window(self):
        """_enrich_results appends the right suffix to the right primary card."""
        collector = AnthropicCollector()

        primary = [
            {"window_type": "session", "detail": "5h detail"},
            {"window_type": "weekly", "detail": "7d detail"},
        ]
        enrichment = [
            {
                "window_type": "session",
                "_enrichment_detail": "in:1k out:500",
                "totals": {},
                "_fallback_card": {},
            },
            {
                "window_type": "weekly",
                "_enrichment_detail": "in:10k out:5k",
                "totals": {},
            },
        ]

        result = collector._enrich_results(primary, enrichment)

        by_window = {r["window_type"]: r for r in result}
        assert "in:1k out:500" in by_window["session"]["detail"]
        assert "in:10k out:5k" in by_window["weekly"]["detail"]

    def test_enrich_results_no_primary_returns_empty(self):
        """Enrichment does not promote fallback when primary is empty."""
        collector = AnthropicCollector()

        enrichment = [
            {
                "window_type": "session",
                "_enrichment_detail": "in:1k",
                "totals": {},
                "token_usage": {"input": 1000, "output": 0, "total": 1000},
                "msgs": 1,
            },
        ]

        result = collector._enrich_results(None, enrichment)

        assert result == []

    def test_enrich_results_error_enrichment_returns_primary(self):
        """When enrichment contains an error card, primary is returned unchanged."""
        collector = AnthropicCollector()

        primary = [{"window_type": "session", "detail": "original detail", "remaining": "50%"}]
        error_enrichment = [{"remaining": "ERR", "detail": "error"}]

        result = collector._enrich_results(primary, error_enrichment)

        assert result is primary
        assert result[0]["detail"] == "original detail"


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

            with patch("app.services.collectors.oauth_base.os.path.exists", return_value=False):
                result = await collector.collect(mock_http_client)

        # Should return empty list or fallback to logs
        assert isinstance(result, list)


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
        """Test that GitHub 429 responses set retry-after for SmartCollector."""
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
        assert collector._last_retry_after == 60.0

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
                "app.services.collectors.github.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=({"api_key": "scoped_token"}, {"source": "sidecar"}),
            ) as mock_get_token,
            patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        ):
            token = await collector._get_token()

        assert token == "scoped_token"
        mock_get_token.assert_awaited_once_with("github", account_id="acc_a")

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
                "app.services.collectors.github.token_cache.get_with_metadata",
                new_callable=AsyncMock,
            ) as mock_get_token,
            patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        ):
            token = await collector._get_token()

        assert token is None
        mock_get_token.assert_not_awaited()


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
        assert result[0].get("variant") == "Codex"
        assert "PLUS" in str(result[0].get("detail", ""))
        assert "%" in str(result[0].get("remaining", ""))

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="browser-cookie / local fallback moved to sidecar")
    async def test_collect_fallback_to_local_logs(self, mock_http_client):
        """Test fallback to local logs when API fails."""
        collector = ChatGPTCollector()

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 500
        mock_http_client.get.return_value = mock_response

        with patch("app.services.collectors.chatgpt_oauth.settings") as mock_settings:
            mock_settings.CHATGPT_SESSIONS_DIR = "/fake/sessions"

            with patch("builtins.open", side_effect=FileNotFoundError):
                result = await collector.collect(mock_http_client)

        # Should return error card if both API and logs fail
        assert isinstance(result, list)

    @pytest.mark.skip(reason="local strategy moved to sidecar")
    def test_chatgpt_codex_sums_all_messages(self, tmp_path):
        """_process_codex_sessions should sum usage from ALL interactions (Total Consumption)."""
        from app.services.collectors.chatgpt import ChatGPTCollector

        collector = ChatGPTCollector()

        # Two turns in one session.
        # msg1: input 100, output 50
        # msg2: input 120 (billed 120, not 20), output 60
        # Total Consumption should be: Input 220, Output 110, Total 330.
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        reset_at = now + timedelta(days=5)
        ts_1 = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        ts_2 = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

        entries = [
            # Turn 1
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-4o"}, "timestamp": ts_1}),
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": ts_1,
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 50}},
                        "rate_limits": {
                            "primary": {"resets_at": reset_at.timestamp(), "window_minutes": 10080}
                        },
                    },
                }
            ),
            # Turn 2
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": ts_2,
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 120, "output_tokens": 60}},
                        "rate_limits": {
                            "primary": {"resets_at": reset_at.timestamp(), "window_minutes": 10080}
                        },
                    },
                }
            ),
        ]

        fpath = tmp_path / "codex-test.jsonl"
        fpath.write_text("\n".join(entries) + "\n")

        result = collector._process_codex_sessions([str(fpath)])
        assert len(result) == 1
        enriched = result[0]

        assert enriched["token_usage"]["input"] == 220
        assert enriched["token_usage"]["output"] == 110
        assert enriched["token_usage"]["total"] == 330
        assert "in:220, out:110" in enriched["_enrichment_detail"]

    @pytest.mark.skip(reason="local strategy moved to sidecar")
    def test_chatgpt_codex_rolls_forward_old_window(self, tmp_path):
        """_process_codex_sessions should roll forward an old resets_at to find the current window cutoff."""
        from datetime import UTC, datetime, timedelta

        from app.services.collectors.chatgpt import ChatGPTCollector

        collector = ChatGPTCollector()

        now = datetime.now(UTC)
        # Log event is from 8 days ago.
        # old_reset is 7.5 days ago.
        # If we roll it forward 7 days (10080 min), the new reset is 0.5 days ago.
        # The cutoff is 7 days before that -> 7.5 days ago.
        # So an event from 8 days ago is BEFORE the cutoff (correctly filtered).
        old_reset = now - timedelta(days=7.5)
        ts_old = (now - timedelta(days=8)).isoformat().replace("+00:00", "Z")

        entries = [
            json.dumps(
                {"type": "turn_context", "payload": {"model": "gpt-4o"}, "timestamp": ts_old}
            ),
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": ts_old,
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 50}},
                        "rate_limits": {
                            "primary": {"resets_at": old_reset.timestamp(), "window_minutes": 10080}
                        },
                    },
                }
            ),
        ]

        fpath = tmp_path / "codex-test-old.jsonl"
        fpath.write_text("\n".join(entries) + "\n")

        # The old event should be filtered out because it belongs to a previous window
        result = collector._process_codex_sessions([str(fpath)])
        assert len(result) == 0

    @pytest.mark.skip(reason="local strategy moved to sidecar")
    def test_chatgpt_codex_uses_primary_metadata_cutoff(self, tmp_path):
        """_process_codex_sessions should use the primary metadata reset_at for the cutoff."""
        from datetime import UTC, datetime, timedelta

        from app.services.collectors.chatgpt import ChatGPTCollector

        collector = ChatGPTCollector()
        now = datetime.now(UTC)

        # Set the primary reset to the future
        primary_reset = now + timedelta(days=2)
        collector._primary_reset_at = primary_reset

        # Log event is from 1 hour ago.
        # Primary reset is 2 days in the future.
        # Window is 7 days.
        # Cutoff is 5 days ago.
        # Event from 1 hour ago is AFTER cutoff (correctly included).
        ts_recent = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

        entries = [
            json.dumps(
                {"type": "turn_context", "payload": {"model": "gpt-4o"}, "timestamp": ts_recent}
            ),
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": ts_recent,
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 50}},
                        "rate_limits": {
                            # Note: The log file contains a VERY old resets_at that would incorrectly put the event in a past window
                            # if it were rolled forward using the 10080 min logic blindly without primary metadata.
                            "primary": {
                                "resets_at": (now - timedelta(days=20)).timestamp(),
                                "window_minutes": 10080,
                            }
                        },
                    },
                }
            ),
        ]

        fpath = tmp_path / "codex-test-primary.jsonl"
        fpath.write_text("\n".join(entries) + "\n")

        result = collector._process_codex_sessions([str(fpath)])
        assert len(result) == 1
        assert result[0]["token_usage"]["total"] == 150


class TestOpenCodeCollector:
    """Test suite for OpenCode collector."""

    @pytest.mark.asyncio
    async def test_opencode_state_persistence(self, tmp_path):
        """OpenCodeCollector saves and loads window state from file."""
        import json
        from datetime import UTC, datetime, timedelta

        # 1. Setup mock state
        data_dir = tmp_path / "runway_data_persistence"
        data_dir.mkdir()
        state_file = data_dir / "opencode_state_default.json"

        fixed_reset = datetime.now(UTC) + timedelta(days=5)
        mock_info = {
            "weeklyUsage": {
                "cutoff": (fixed_reset - timedelta(days=7)).isoformat(),
                "is_fixed": True,
                "reset_at": fixed_reset.isoformat(),
            }
        }
        with open(state_file, "w") as f:
            json.dump(mock_info, f)

        # 2. Verify loading in __init__
        with patch("app.services.collectors.opencode.settings") as mock_settings:
            mock_settings.data_dir = str(data_dir)
            collector = OpenCodeCollector()

            assert collector._last_window_info is not None
            assert "weeklyUsage" in collector._last_window_info
            # Verify it converted back to datetime
            assert isinstance(collector._last_window_info["weeklyUsage"]["cutoff"], datetime)
            assert collector._last_window_info["weeklyUsage"]["is_fixed"] is True

            # 3. Verify saving
            new_reset = datetime.now(UTC) + timedelta(days=6)
            new_info = {
                "rollingUsage": {
                    "cutoff": (new_reset - timedelta(hours=5)),
                    "is_fixed": False,
                    "reset_at": None,
                }
            }
            collector._save_persisted_state(new_info)

            with open(state_file) as f:
                saved = json.load(f)
                assert "rollingUsage" in saved
                assert saved["rollingUsage"]["is_fixed"] is False
                assert "cutoff" in saved["rollingUsage"]
                assert isinstance(saved["rollingUsage"]["cutoff"], str)

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
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

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    async def test_get_opencode_tui_per_window_enrichment(self, tmp_path):
        """_get_opencode_tui emits enrichment dicts, not plain cards."""
        import json
        import sqlite3
        from datetime import UTC, datetime, timedelta

        db_path = str(tmp_path / "opencode.db")
        # Ensure a clean state directory for this test
        state_dir = tmp_path / "runway_data_tui"
        state_dir.mkdir()

        now_ms = int(datetime.now(UTC).timestamp() * 1000)

        def _row(
            offset_hours: float,
            model: str,
            cost: float,
            t_in: int,
            t_out: int,
            cache_r: int,
            cache_w: int,
            parent_id: str,
            provider_id: str = "opencode-go",
        ) -> dict:
            ts = int((datetime.now(UTC) - timedelta(hours=offset_hours)).timestamp() * 1000)
            return {
                "time_created": ts,
                "data": json.dumps(
                    {
                        "role": "assistant",
                        "cost": cost,
                        "tokens": {
                            "input": t_in,
                            "output": t_out,
                            "reasoning": 0,
                            "cache": {"read": cache_r, "write": cache_w},
                        },
                        "modelID": model,
                        "parentID": parent_id,
                        "providerID": provider_id,
                    }
                ),
            }

        # Go tier: opencode-go (counts toward limits)
        go_rows = [
            _row(1, "qwen3.5-plus", 1.50, 10000, 500, 5000, 200, "conv-a"),
            _row(3, "qwen3.5-plus", 2.00, 20000, 800, 0, 0, "conv-b"),
        ]
        # Free tier: opencode (unlimited)
        free_rows = [
            _row(100, "minimax-m2.5-free", 0.0, 3000, 100, 0, 0, "conv-c", provider_id="opencode"),
        ]
        rows = go_rows + free_rows
        # go_rows[0] and go_rows[1] are inside 5h; go_rows inside 7d and 30d
        # free_rows[0] is 100h ago → outside 5h window, but inside 7d (100h < 168h)

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE message (time_created INTEGER, data TEXT)")
        conn.execute("CREATE TABLE account (email TEXT)")
        conn.execute("INSERT INTO account (email) VALUES (?)", ("opencode@example.com",))
        for r in rows:
            conn.execute("INSERT INTO message VALUES (?, ?)", (r["time_created"], r["data"]))
        conn.commit()
        conn.close()

        with patch("app.services.collectors.opencode.settings") as mock_settings:
            mock_settings.OPENCODE_DB_PATH = db_path
            # Use the clean state directory
            mock_settings.data_dir = str(state_dir)
            collector = OpenCodeCollector()
            result = await collector._get_opencode_tui()

        assert collector.account_label == "opencode@example.com"
        assert len(result) == 4
        by_wt = {e["window_type"]: e for e in result}
        assert set(by_wt.keys()) == {"session", "weekly", "monthly", "rolling"}

        free_card = by_wt.get("rolling")
        assert free_card is not None
        assert free_card.get("variant") == "Free"
        assert "token_usage" in free_card
        assert "by_model" in free_card
        assert "msgs" in free_card
        assert "Lifetime:" in free_card["_enrichment_detail"]

        fh = by_wt["session"]
        assert fh.get("_enrichment_detail")
        assert "$" in fh["_enrichment_detail"]
        assert "qwen" in fh["_enrichment_detail"]
        assert "cache_w:0" not in fh["_enrichment_detail"]
        assert "token_usage" in fh
        assert "by_model" in fh
        assert "msgs" in fh

        # Session window: only go_rows[0] and go_rows[1] (free_rows[0] is 100h ago)
        assert fh["totals"]["msgs"] == 2
        assert fh["totals"]["convos"] == 2
        assert fh["msgs"] == 2
        assert fh["token_usage"]["input"] == 30000
        assert fh["token_usage"]["total"] == 30000 + 1300 + 0  # in + out + reasoning
        assert "qwen3.5-plus" in fh["by_model"]
        assert "tokens" in fh["by_model"]["qwen3.5-plus"]
        assert fh["by_model"]["qwen3.5-plus"]["tokens"]["total"] > 0

        # Weekly window: only Go rows (100h < 168h = 7d)
        weekly = by_wt["weekly"]
        assert weekly["totals"]["msgs"] == 2
        assert weekly["totals"]["convos"] == 2
        assert weekly["msgs"] == 2

    @pytest.mark.asyncio
    async def test_enrich_results_matches_by_service_name(self):
        """Enrichment suffix lands on the matching primary card; orphan is dropped."""
        collector = OpenCodeCollector()

        primary = [
            {
                "service_name": "OpenCode",
                "window_type": "session",
                "detail": "$1.00 used · Web API",
                "remaining": "$11.00",
            },
            {
                "service_name": "OpenCode",
                "window_type": "weekly",
                "detail": "$5.00 used · Web API",
                "remaining": "$25.00",
            },
        ]
        enrichment = [
            {
                "service_name": "OpenCode",
                "window_type": "session",
                "_enrichment_detail": "$1.00 | in:10,000",
            },
            {
                "service_name": "OpenCode",
                "window_type": "weekly",
                "_enrichment_detail": "$5.00 | in:50,000",
            },
            {
                "service_name": "OpenCode",
                "window_type": "monthly",
                "_enrichment_detail": "$8.00 | in:80,000",
            },
        ]

        result = collector._enrich_results(primary, enrichment)

        assert len(result) == 2
        fh = next(c for c in result if c["window_type"] == "session")
        assert "in:10,000" in fh["detail"]
        weekly = next(c for c in result if c["window_type"] == "weekly")
        assert "in:50,000" in weekly["detail"]

    @pytest.mark.asyncio
    async def test_enrich_results_no_primary_returns_empty(self):
        """Enrichment does not promote fallback when primary is empty."""
        collector = OpenCodeCollector()

        enrichment = [
            {
                "service_name": "OpenCode",
                "window_type": "session",
                "_enrichment_detail": "$0.00",
                "token_usage": {"input": 0, "output": 0, "total": 0},
                "msgs": 0,
            },
        ]

        result = collector._enrich_results([], enrichment)
        assert result == []

    @pytest.mark.asyncio
    async def test_enrich_results_error_enrichment_returns_primary(self):
        """Error-shaped or empty enrichment leaves primary untouched."""
        collector = OpenCodeCollector()

        primary = [
            {
                "service_name": "OpenCode",
                "window_type": "session",
                "detail": "$1.00 used",
                "remaining": "$11.00",
            }
        ]

        assert collector._enrich_results(primary, []) == primary
        assert collector._enrich_results(primary, [{"remaining": "ERR"}]) == primary

    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    def test_parse_usage_records_classifies_go_free_api(self):
        """Records with enrichment→go, cost==0→free, cost>0+no-enrichment→api."""
        collector = OpenCodeCollector()

        # Build a minimal /usage page HTML snippet with one record of each type.
        # Uses the exact field order from the live page.
        def _rec(uid, model, provider, cost, enrichment):
            return (
                f"$R[9]={{"
                f'id:"usg_{uid}",'
                f'workspaceID:"wrk_TEST",'
                f'timeCreated:new Date("2026-04-20T10:00:00.000Z"),'
                f'timeUpdated:new Date("2026-04-20T10:00:00.100Z"),'
                f"timeDeleted:null,"
                f'model:"{model}",'
                f'provider:"{provider}",'
                f"inputTokens:1000,"
                f"outputTokens:200,"
                f"reasoningTokens:null,"
                f"cacheReadTokens:500,"
                f"cacheWrite5mTokens:0,"
                f"cacheWrite1hTokens:null,"
                f"cost:{cost},"
                f'keyID:"key_AAA",'
                f'sessionID:"",'
                f"enrichment:{enrichment}"
                f"}}"
            )

        html = (
            _rec("GO1", "claude-sonnet-4-6", "anthropic", 970321, '$R[10]={plan:"lite"}')
            + _rec("FR1", "minimax-m2.5-free", "minimax", 0, "null")
            + _rec("AP1", "gpt-4o", "openai", 500000, "null")
        )

        records = collector._parse_usage_records(html)
        assert len(records) == 3

        by_source = {r["source"]: r for r in records}
        assert set(by_source) == {"go", "free", "api"}
        assert abs(by_source["go"]["cost_usd"] - 970321 * 1e-8) < 1e-10
        assert by_source["free"]["cost_usd"] == 0.0
        assert by_source["go"]["model_short"] == "sonnet"

    def test_parse_usage_records_empty_html(self):
        """No records in plain HTML → empty list, no error."""
        collector = OpenCodeCollector()
        assert collector._parse_usage_records("<html><body>nothing here</body></html>") == []

    def test_build_usage_breakdown_windows(self):
        """go records are counted into the correct time windows."""
        collector = OpenCodeCollector()
        now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)

        def _rec(hours_ago, source, cost=0.01, model="sonnet"):
            return {
                "ts": now - timedelta(hours=hours_ago),
                "model": f"claude-{model}-4-6",
                "model_short": model,
                "source": source,
                "input": 1000,
                "output": 100,
                "reasoning": 0,
                "cache_read": 0,
                "cost_usd": cost,
            }

        records = [
            _rec(1, "go", 0.01),  # inside all windows
            _rec(6, "go", 0.02),  # outside 5h, inside 7d and 30d
            _rec(200, "go", 0.04),  # outside 7d, inside 30d
            _rec(800, "go", 0.08),  # outside all windows
            _rec(0.5, "free"),  # free
            _rec(0.5, "api", 0.05),  # api
        ]

        bd = collector._build_usage_breakdown(records, now, {})

        assert bd["go"]["5h"]["msgs"] == 1
        assert abs(bd["go"]["5h"]["cost"] - 0.01) < 1e-9
        assert bd["go"]["7d"]["msgs"] == 2
        assert bd["go"]["30d"]["msgs"] == 3
        assert bd["free"]["lifetime"]["msgs"] == 1
        assert bd["api"]["lifetime"]["msgs"] == 1
        assert abs(bd["api"]["lifetime"]["cost"] - 0.05) < 1e-9

        # by_model token accumulation
        assert "sonnet" in bd["go"]["5h"]["by_model"]
        assert bd["go"]["5h"]["by_model"]["sonnet"]["tokens"]["input"] == 1000
        assert bd["go"]["5h"]["by_model"]["sonnet"]["tokens"]["output"] == 100
        assert bd["go"]["5h"]["by_model"]["sonnet"]["tokens"]["total"] == 1100
        assert bd["go"]["7d"]["by_model"]["sonnet"]["tokens"]["input"] == 2000
        assert bd["go"]["7d"]["by_model"]["sonnet"]["tokens"]["total"] == 2200
        assert bd["free"]["lifetime"]["by_model"]["sonnet"]["tokens"]["input"] == 1000
        assert bd["api"]["lifetime"]["by_model"]["sonnet"]["tokens"]["input"] == 1000

    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    def test_parse_usage_data_enriches_go_cards_and_emits_extra_cards(self):
        """_parse_usage_data with a breakdown enriches detail and adds Free/API cards."""
        collector = OpenCodeCollector()

        # Minimal /go page text that produces the three standard Go cards
        go_text = (
            "s3ntin3l8@gmail.com "
            "rollingUsage:{usagePercent:50,resetInSec:3600} "
            "weeklyUsage:{usagePercent:30,resetInSec:86400} "
            "monthlyUsage:{usagePercent:20,resetInSec:2592000}"
        )

        breakdown = {
            "go": {
                "5h": {
                    "cost": 2.50,
                    "msgs": 10,
                    "tokens": {"input": 50000, "output": 5000, "reasoning": 0, "cache_read": 0},
                    "by_model": {"sonnet": {"cost": 2.50, "msgs": 10}},
                },
                "7d": {
                    "cost": 8.00,
                    "msgs": 40,
                    "tokens": {"input": 200000, "output": 20000, "reasoning": 0, "cache_read": 0},
                    "by_model": {"sonnet": {"cost": 8.00, "msgs": 40}},
                },
                "30d": {
                    "cost": 10.00,
                    "msgs": 50,
                    "tokens": {"input": 250000, "output": 25000, "reasoning": 0, "cache_read": 0},
                    "by_model": {"sonnet": {"cost": 10.00, "msgs": 50}},
                },
            },
            "free": {
                "lifetime": {
                    "cost": 0.0,
                    "msgs": 25,
                    "tokens": {"input": 100000, "output": 5000, "reasoning": 0, "cache_read": 0},
                    "by_model": {"m2.5-free": {"cost": 0.0, "msgs": 25}},
                }
            },
            "api": {
                "lifetime": {
                    "cost": 0.30,
                    "msgs": 3,
                    "tokens": {"input": 5000, "output": 500, "reasoning": 0, "cache_read": 0},
                    "by_model": {"gpt-4o": {"cost": 0.30, "msgs": 3}},
                }
            },
        }

        cards = collector._parse_usage_data(go_text, "wrk_TEST", breakdown)

        # 3 Go + 1 Free + 1 API
        assert len(cards) == 5

        service_names = {c["service_name"] for c in cards}
        assert "OpenCode" in service_names

        # Go (session) card detail should contain the per-model enrichment
        five_h = next(c for c in cards if c.get("window_type") == "session")
        assert "sonnet" in five_h["detail"]
        assert "$2.50" in five_h["detail"]

        # Monthly card window_type must be "monthly" — drives the breakdown_key_for
        # lookup. Mismatch here silently breaks per-model enrichment, sidecar
        # aggregation, and history continuity.
        monthly_card = next(
            c for c in cards if c.get("window_type") == "monthly" and c["limit_value"] == 60.0
        )
        assert monthly_card["service_name"] == "OpenCode"

        # Free card: is_unlimited=True shows token count instead of infinity
        free_card = next(c for c in cards if c.get("variant") == "Free")
        assert free_card["is_unlimited"] is True
        assert free_card["health"] == "good"
        assert "tokens" in free_card["remaining"]  # e.g. "105,000 tokens"
        assert "Free tier" in free_card["detail"]

        # API card: is_unlimited=False so detail renders as card subtitle
        api_card = next(c for c in cards if c.get("variant") == "API")
        assert api_card["is_unlimited"] is False
        assert "$0.30" in api_card["detail"] or "0.30" in api_card["remaining"]

        # Tier badges
        go_cards = [c for c in cards if c.get("tier") == "Go"]
        assert all(c.get("tier") == "Go" for c in go_cards)
        assert free_card.get("tier") == "Free"
        assert api_card.get("tier") == "API"

        # input_source defaults to "server"
        assert all(c.get("input_source") == "server" for c in cards)

    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    def test_parse_usage_data_input_source_config(self):
        """input_source='config' propagates to all cards when cookie came from UI."""
        collector = OpenCodeCollector()

        go_text = (
            "s3ntin3l8@gmail.com "
            "rollingUsage:{usagePercent:50,resetInSec:3600} "
            "weeklyUsage:{usagePercent:30,resetInSec:86400} "
            "monthlyUsage:{usagePercent:20,resetInSec:2592000}"
        )
        breakdown = {
            "go": {
                "5h": {
                    "cost": 1.0,
                    "msgs": 5,
                    "tokens": {"input": 1000, "output": 100, "reasoning": 0, "cache_read": 0},
                    "by_model": {"sonnet": {"cost": 1.0, "msgs": 5}},
                },
                "7d": {
                    "cost": 2.0,
                    "msgs": 10,
                    "tokens": {"input": 2000, "output": 200, "reasoning": 0, "cache_read": 0},
                    "by_model": {},
                },
                "30d": {
                    "cost": 3.0,
                    "msgs": 15,
                    "tokens": {"input": 3000, "output": 300, "reasoning": 0, "cache_read": 0},
                    "by_model": {},
                },
            },
            "free": {
                "lifetime": {
                    "cost": 0.0,
                    "msgs": 5,
                    "tokens": {"input": 5000, "output": 500, "reasoning": 0, "cache_read": 0},
                    "by_model": {},
                }
            },
            "api": {
                "lifetime": {
                    "cost": 0.0,
                    "msgs": 0,
                    "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0},
                    "by_model": {},
                }
            },
        }

        cards = collector._parse_usage_data(go_text, "wrk_TEST", breakdown, input_source="config")
        assert all(c.get("input_source") == "config" for c in cards)

    def test_parse_usage_data_no_breakdown_returns_three_go_cards(self):
        """Without a breakdown, _parse_usage_data still returns the 3 Go cards unchanged."""
        collector = OpenCodeCollector()

        go_text = (
            "rollingUsage:{usagePercent:40,resetInSec:3600} "
            "weeklyUsage:{usagePercent:20,resetInSec:86400} "
            "monthlyUsage:{usagePercent:10,resetInSec:2592000}"
        )

        cards = collector._parse_usage_data(go_text, "wrk_TEST", breakdown=None)
        assert len(cards) == 3
        assert all("OpenCode" in c["service_name"] for c in cards)

    def test_parse_usage_data_zero_usage_skips_free_api_cards(self):
        """Free/API cards are omitted when msgs==0."""
        collector = OpenCodeCollector()

        go_text = (
            "rollingUsage:{usagePercent:10,resetInSec:100} "
            "weeklyUsage:{usagePercent:5,resetInSec:200} "
            "monthlyUsage:{usagePercent:3,resetInSec:300}"
        )

        empty_bucket = {
            "cost": 0.0,
            "msgs": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0},
            "by_model": {},
        }
        breakdown = {
            "go": {"5h": empty_bucket, "7d": empty_bucket, "30d": empty_bucket},
            "free": {"lifetime": empty_bucket},
            "api": {"lifetime": empty_bucket},
        }

        cards = collector._parse_usage_data(go_text, "wrk_TEST", breakdown)
        assert len(cards) == 3  # only Go cards, no Free or API

    @pytest.mark.asyncio
    async def test_get_workspace_id_from_env(self, mock_http_client):
        """OPENCODE_WORKSPACE_ID env var bypasses the network call."""
        collector = OpenCodeCollector()
        with patch.dict(os.environ, {"OPENCODE_WORKSPACE_ID": "wrk_env_123"}, clear=False):
            result = await collector._get_workspace_id(mock_http_client, {})
        assert result == "wrk_env_123"

    @pytest.mark.asyncio
    async def test_get_workspace_id_from_env_url(self, mock_http_client):
        """Env var with full URL format extracts just the workspace ID."""
        collector = OpenCodeCollector()
        with patch.dict(
            os.environ,
            {"OPENCODE_WORKSPACE_ID": "https://opencode.ai/workspace/wrk_url_456/go"},
            clear=False,
        ):
            result = await collector._get_workspace_id(mock_http_client, {})
        assert result == "wrk_url_456"

    @pytest.mark.asyncio
    async def test_get_workspace_id_from_response(self, mock_http_client):
        """Workspace ID and email are extracted from the JS response."""
        collector = OpenCodeCollector()

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.text = 'someJS({id:"wrk_resp789",name:"Test"});user@example.com '

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ),
        ):
            result = await collector._get_workspace_id(mock_http_client, {})

        assert result == "wrk_resp789"
        assert collector.account_label == "user@example.com"

    @pytest.mark.asyncio
    async def test_get_workspace_id_post_fallback(self, mock_http_client):
        """GET 404 falls back to POST with empty body."""
        collector = OpenCodeCollector()

        get_resp = MagicMock(spec=httpx.Response)
        get_resp.status_code = 404

        post_resp = MagicMock(spec=httpx.Response)
        post_resp.status_code = 200
        post_resp.text = 'id:"wrk_post999"'

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[get_resp, post_resp],
            ) as mock_retry,
        ):
            result = await collector._get_workspace_id(mock_http_client, {})

        assert result == "wrk_post999"
        assert mock_retry.call_count == 2

    @pytest.mark.asyncio
    async def test_get_workspace_id_failure_returns_none(self, mock_http_client):
        """Non-200 on both GET and POST returns None."""
        collector = OpenCodeCollector()

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 500

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ),
        ):
            result = await collector._get_workspace_id(mock_http_client, {})

        assert result is None

    @pytest.mark.asyncio
    async def test_get_subscription_data_success(self, mock_http_client):
        """Subscription data page is returned as text."""
        collector = OpenCodeCollector()

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.text = "rollingUsage:{usagePercent:10,resetInSec:3600}"

        with patch(
            "app.services.collectors.opencode.http_request_with_retry",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            result = await collector._get_subscription_data(mock_http_client, {}, "wrk_123")
        assert result == "rollingUsage:{usagePercent:10,resetInSec:3600}"

    @pytest.mark.asyncio
    async def test_get_subscription_data_failure_returns_none(self, mock_http_client):
        """Non-200 response returns None."""
        collector = OpenCodeCollector()

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 403

        with patch(
            "app.services.collectors.opencode.http_request_with_retry",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            result = await collector._get_subscription_data(mock_http_client, {}, "wrk_123")
        assert result is None

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    async def test_get_opencode_web_full_flow(self, mock_http_client, tmp_path):
        """End-to-end web API collection with mocked responses."""
        collector = OpenCodeCollector()
        data_dir = tmp_path / "runway_data_web"
        data_dir.mkdir()

        with patch("app.services.collectors.opencode.settings") as mock_settings:
            mock_settings.data_dir = str(data_dir)

            # 1. Workspace discovery
            ws_resp = MagicMock(spec=httpx.Response)
            ws_resp.status_code = 200
            ws_resp.text = 'id:"wrk_flow111";user@flow.com'

            # 2. Subscription /go page
            sub_resp = MagicMock(spec=httpx.Response)
            sub_resp.status_code = 200
            sub_resp.text = (
                "user@flow.com "
                "rollingUsage:{usagePercent:25,resetInSec:3600} "
                "weeklyUsage:{usagePercent:40,resetInSec:345600} "
                "monthlyUsage:{usagePercent:15,resetInSec:864000}"
            )

            # 3. /usage page (enrichment)
            usage_resp = MagicMock(spec=httpx.Response)
            usage_resp.status_code = 200
            usage_resp.text = ""  # empty — no per-model breakdown

            with (
                patch(
                    "app.services.collectors.opencode.get_opencode_session_cookie",
                    return_value="session_cookie_123",
                ),
                patch(
                    "app.services.collectors.opencode.http_request_with_retry",
                    new_callable=AsyncMock,
                    side_effect=[ws_resp, sub_resp, usage_resp],
                ),
            ):
                result = await collector._get_opencode_web(mock_http_client)

        assert len(result) == 3
        by_wt = {c["window_type"]: c for c in result}
        assert set(by_wt.keys()) == {"session", "weekly", "monthly"}

        session = by_wt["session"]
        assert session["used_value"] == 3.0  # 25% of $12
        assert session["remaining"] == "$9.00"
        assert session["provider_id"] == "opencode"
        assert session["input_source"] == "server"

        weekly = by_wt["weekly"]
        assert weekly["used_value"] == 12.0  # 40% of $30
        assert weekly["remaining"] == "$18.00"

        monthly = by_wt["monthly"]
        assert monthly["used_value"] == 9.0  # 15% of $60
        assert monthly["remaining"] == "$51.00"

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    async def test_get_opencode_web_no_cookie_returns_empty(self, mock_http_client):
        """When no session cookie is available, return empty list immediately."""
        collector = OpenCodeCollector()

        with (
            patch(
                "app.services.collectors.opencode.get_opencode_session_cookie",
                return_value=None,
            ),
            patch(
                "app.services.collectors.opencode.token_cache.get_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await collector._get_opencode_web(mock_http_client)

        assert result == []

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    async def test_get_opencode_web_workspace_failure_returns_empty(self, mock_http_client):
        """If workspace ID cannot be discovered, return empty list."""
        collector = OpenCodeCollector()

        ws_resp = MagicMock(spec=httpx.Response)
        ws_resp.status_code = 200
        ws_resp.text = "no workspace id here"

        with (
            patch(
                "app.services.collectors.opencode.get_opencode_session_cookie",
                return_value="session_cookie_123",
            ),
            patch.dict(os.environ, {}, clear=True),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=ws_resp,
            ),
        ):
            result = await collector._get_opencode_web(mock_http_client)

        assert result == []

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    async def test_negative_tokens_clamped_to_zero(self):
        """Negative raw token values are clamped to zero."""
        collector = OpenCodeCollector()

        html = (
            '$R[1]={id:"usg_NEG",workspaceID:"wrk_1",'
            'timeCreated:new Date("2026-04-20T10:00:00.000Z"),'
            'timeUpdated:new Date("2026-04-20T10:00:00.100Z"),'
            "timeDeleted:null,"
            'model:"claude-sonnet-4-6",provider:"anthropic",'
            "inputTokens:-1000,"
            "outputTokens:-200,"
            "reasoningTokens:-50,"
            "cacheReadTokens:-500,"
            "cacheWrite5mTokens:0,"
            "cacheWrite1hTokens:null,"
            "cost:-970321,"
            'keyID:"key_AAA",sessionID:"",'
            'enrichment:$R[2]={plan:"lite"}'
            "}"
        )

        records = collector._parse_usage_records(html)
        assert len(records) == 1
        r = records[0]
        assert r["input"] == 0
        assert r["output"] == 0
        assert r["reasoning"] == 0
        assert r["cache_read"] == 0
        assert r["cost_usd"] == 0.0

    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    def test_free_api_cards_have_provider_id(self):
        """Free and API cards emitted by _build_free_api_card carry provider_id."""
        collector = OpenCodeCollector()

        free_card = collector._build_free_api_card(
            "free",
            {
                "cost": 0.0,
                "msgs": 5,
                "tokens": {"input": 1000, "output": 100, "reasoning": 0, "cache_read": 0},
                "by_model": {"m2.5-free": {"cost": 0.0, "msgs": 5}},
            },
            "wrk_123",
            "user@example.com",
            "2026-04-20T12:00:00+00:00",
        )
        assert free_card["provider_id"] == "opencode"

        api_card = collector._build_free_api_card(
            "api",
            {
                "cost": 0.30,
                "msgs": 3,
                "tokens": {"input": 5000, "output": 500, "reasoning": 0, "cache_read": 0},
                "by_model": {"gpt-4o": {"cost": 0.30, "msgs": 3}},
            },
            "wrk_123",
            "user@example.com",
            "2026-04-20T12:00:00+00:00",
        )
        assert api_card["provider_id"] == "opencode"


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
        token_card = next((c for c in result if c.get("variant") == "Tokens"), None)
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
        assert any(c.get("variant") == "Tokens" for c in result)
        assert any(c.get("variant") == "Time" for c in result)

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
        assert any(card.get("window_type") == "weekly" for card in result)
        assert any(card.get("window_type") == "session" for card in result)
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
    @pytest.mark.skip(reason="browser-cookie / local fallback moved to sidecar")
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
        assert result[0]["service_name"] == "OpenRouter"
        assert result[0].get("variant") == "Credits"
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
        services = {c.get("variant"): c for c in result}
        assert "Credits" in services
        assert "Key Limit" in services
        key_card = services["Key Limit"]
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
        assert result[0]["service_name"] == "OpenRouter"
        assert result[0].get("variant") == "Credits"

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
        assert result[0]["service_name"] == "OpenRouter"
        assert result[0].get("variant") == "Credits"

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
        assert result[0]["service_name"] == "OpenRouter"
        assert result[0].get("variant") == "Credits"

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
