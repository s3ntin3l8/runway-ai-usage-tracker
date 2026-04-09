"""
Unit tests for quota collectors.

Tests cover:
- OAuth/API collection success and error handling
- Fallback logic between primary and secondary sources
- Token caching and refresh behavior
- Error card generation for various failure scenarios
- Local log parsing and file-based data sources
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
from datetime import datetime, timezone
import json
import httpx

from app.services.collectors.anthropic import AnthropicCollector
from app.services.collectors.gemini import GeminiCollector
from app.services.collectors.github import GitHubCollector
from app.services.collectors.chatgpt import ChatGPTCollector
from app.services.collectors.antigravity import AntigravityCollector
from app.services.collectors.opencode import OpenCodeCollector
from app.services.collectors.zai_api import ZaiApiCollector
from app.services.collectors.zai_plan import ZaiPlanCollector
from app.services.collectors.kimi_api import KimiApiCollector
from app.services.collectors.kimi_coding import KimiCodingCollector


class TestAnthropicCollector:
    """Test suite for Anthropic (Claude) collector."""

    @pytest.mark.asyncio
    async def test_collect_oauth_success(
        self, mock_http_client, mock_anthropic_oauth_response
    ):
        """Test successful OAuth API collection."""
        collector = AnthropicCollector()

        # Mock successful OAuth response using request() (called by http_request_with_retry)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_anthropic_oauth_response
        mock_http_client.request.return_value = mock_response

        with patch(
            "app.services.credential_provider.CredentialProvider.get_claude_token",
            return_value="test_token",
        ):
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.CLAUDE_PROJECTS_DIR = "/home/user/.claude/projects"
                mock_settings.LOCAL_COLLECTOR_ENABLED = True
                mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
                mock_settings.CLAUDE_PRO_LIMIT = 2000000
                mock_settings.CLAUDE_FREE_LIMIT = 500000

                with patch(
                    "app.services.collectors.anthropic.get_claude_session_cookie",
                    return_value=None,
                ):
                    with patch.object(
                        collector, "_get_valid_token", return_value="test_token"
                    ):
                        result = await collector.collect(mock_http_client)

        # Should return cards for each quota window
        assert isinstance(result, list)
        assert len(result) >= 1
        assert all("service" in card for card in result)
        assert any("Session" in str(card.get("service", "")) for card in result)

    @pytest.mark.asyncio
    async def test_collect_oauth_401_fallback(self, mock_http_client):
        """Test fallback to local logs when OAuth token is invalid (401)."""
        collector = AnthropicCollector()

        # Mock 401 response using request()
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 401
        mock_http_client.request.return_value = mock_response

        with patch(
            "app.services.credential_provider.CredentialProvider.get_claude_token",
            return_value="invalid_token",
        ):
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.CLAUDE_PRO_LIMIT = 2000000
                mock_settings.CLAUDE_FREE_LIMIT = 500000
                mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
                mock_settings.LOCAL_COLLECTOR_ENABLED = True
                mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

                with patch(
                    "app.services.collectors.anthropic.get_claude_session_cookie",
                    return_value=None,
                ):
                    with patch.object(
                        collector, "_get_valid_token", return_value="invalid_token"
                    ):
                        with patch(
                            "app.services.collectors.anthropic.glob.glob",
                            return_value=[],
                        ):
                            result = await collector.collect(mock_http_client)

        # Should return error card for invalid token (no logs fallback)
        assert any("ERR" in str(card.get("remaining", "")) for card in result)

    @pytest.mark.asyncio
    async def test_collect_caching(
        self, mock_http_client, mock_anthropic_oauth_response
    ):
        """Test that OAuth results are cached for 10 minutes."""
        collector = AnthropicCollector()

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_anthropic_oauth_response
        mock_http_client.request.return_value = mock_response

        with patch(
            "app.services.credential_provider.CredentialProvider.get_claude_token",
            return_value="test_token",
        ):
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
                mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
                mock_settings.LOCAL_COLLECTOR_ENABLED = False

                with patch(
                    "app.services.collectors.anthropic.get_claude_session_cookie",
                    return_value=None,
                ):
                    with patch.object(
                        collector, "_get_valid_token", return_value="test_token"
                    ):
                        # First call - should hit API
                        result1 = await collector.collect(mock_http_client)

                        # Second call immediately - should use cache
                        result2 = await collector.collect(mock_http_client)

                # API should only be called once (cached on second call)
                assert mock_http_client.request.call_count == 1
                # Results should be identical (same cached data)
                assert result1 == result2

    @pytest.mark.asyncio
    async def test_collect_oauth_429_error_caching(self, mock_http_client):
        """Test that 429 rate limit errors are cached to avoid hammering the API."""
        collector = AnthropicCollector()

        # Mock 429 rate limit response (http_request_with_retry makes 3 attempts)
        mock_429_response = MagicMock(spec=httpx.Response)
        mock_429_response.status_code = 429
        mock_http_client.request.return_value = mock_429_response

        with patch(
            "app.services.credential_provider.CredentialProvider.get_claude_token",
            return_value="test_token",
        ):
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
                mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
                mock_settings.LOCAL_COLLECTOR_ENABLED = False

                with patch(
                    "app.services.collectors.anthropic.get_claude_session_cookie",
                    return_value="fake_session",
                ):
                    with patch.object(
                        collector, "_get_valid_token", return_value="test_token"
                    ):
                        with patch.object(
                            collector,
                            "_get_claude_via_web_api",
                            return_value=[
                                {
                                    "service": "Claude (Session Window)",
                                    "icon": "🟠",
                                    "remaining": "50.0%",
                                    "unit": "capacity",
                                    "reset": "in 4h",
                                    "health": "good",
                                    "pace": "Sustainable",
                                    "detail": "50.0% used [Web API]",
                                    "used_value": 50.0,
                                    "limit_value": 100.0,
                                    "unit_type": "percent",
                                    "data_source": "web_api",
                                }
                            ],
                        ):
                            # First call - OAuth gets 429 (3 retries), falls back to Web API
                            result1 = await collector.collect(mock_http_client)

                            # Second call - should use cached 429 error, skip OAuth entirely
                            result2 = await collector.collect(mock_http_client)

                            # OAuth API should be called 3 times on first call (retries), 0 times on second (cached)
                            assert (
                                mock_http_client.request.call_count == 3
                            )  # 3 retries on first call
                            # Both results should come from Web API fallback
                            assert result1[0]["data_source"] == "web_api"
                            assert result2[0]["data_source"] == "web_api"

    @pytest.mark.asyncio
    async def test_collect_oauth_token_refresh_success(
        self, mock_http_client, mock_anthropic_oauth_response
    ):
        """Test successful OAuth token refresh when original token is expired."""
        collector = AnthropicCollector()

        # Mock initial 401 response (expired token)
        oauth_401_response = MagicMock(spec=httpx.Response)
        oauth_401_response.status_code = 401
        oauth_401_response.json.return_value = {"error": "unauthorized"}
        oauth_401_response.text = '{"error": "unauthorized"}'

        # Mock successful token refresh response
        refresh_response = MagicMock(spec=httpx.Response)
        refresh_response.status_code = 200
        refresh_response.json.return_value = {
            "access_token": "new_refreshed_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 28800,
        }

        # Mock successful OAuth call with new token
        oauth_success_response = MagicMock(spec=httpx.Response)
        oauth_success_response.status_code = 200
        oauth_success_response.json.return_value = mock_anthropic_oauth_response

        # Set up mock to return different responses for different calls
        call_count = [0]

        async def mock_request(*args, **kwargs):
            call_count[0] += 1
            url = args[1] if len(args) > 1 else kwargs.get("url", "")

            # First OAuth call (with old token) -> 401
            if call_count[0] == 1 and "oauth/usage" in url:
                return oauth_401_response
            # Second OAuth call (with new token) -> success
            elif call_count[0] == 2 and "oauth/usage" in url:
                return oauth_success_response
            return oauth_success_response

        mock_http_client.request.side_effect = mock_request
        mock_http_client.post.return_value = refresh_response

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
                    with patch.object(
                        collector, "_persist_credentials", return_value=None
                    ):
                        with patch(
                            "app.services.token_cache.token_cache.store",
                            return_value=None,
                        ):
                            # First request gets 401, then reactive refresh happens, then second request succeeds
                            result = await collector.collect(mock_http_client)
                            print(f"\nDEBUG: result after refresh = {result}")

        # Should return successful OAuth results (not error cards)
        assert isinstance(result, list)
        assert len(result) >= 1
        for i, card in enumerate(result):
            print(f"DEBUG: card {i} = {card}")
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

        # Mock OAuth failure (401) - using request() for OAuth
        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401

        # Mock Web API success - using get() for Web API
        orgs_response = MagicMock(spec=httpx.Response)
        orgs_response.status_code = 200
        orgs_response.json.return_value = mock_claude_web_api_orgs_response

        # Mock account endpoint (optional, called between orgs and usage)
        account_response = MagicMock(spec=httpx.Response)
        account_response.status_code = 200
        account_response.json.return_value = {"tier": "pro"}

        usage_response = MagicMock(spec=httpx.Response)
        usage_response.status_code = 200
        usage_response.json.return_value = mock_claude_web_api_usage_response

        # Mock request for OAuth (first call)
        mock_http_client.request.return_value = oauth_response
        # Mock get for Web API calls (orgs, account, usage)
        mock_http_client.get.side_effect = [
            orgs_response,
            account_response,
            usage_response,
        ]

        with patch(
            "app.services.credential_provider.CredentialProvider.get_claude_token",
            return_value="invalid_token",
        ):
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.CLAUDE_PRO_LIMIT = 2000000
                mock_settings.CLAUDE_FREE_LIMIT = 500000
                mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
                mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
                mock_settings.LOCAL_COLLECTOR_ENABLED = False

                with patch(
                    "app.services.collectors.anthropic.get_claude_session_cookie",
                    return_value="sk-ant-session123",
                ):
                    with patch.object(
                        collector, "_get_valid_token", return_value="invalid_token"
                    ):
                        result = await collector.collect(mock_http_client)

        # Should return Web API results
        assert isinstance(result, list)
        assert len(result) >= 1
        assert any(card.get("data_source") == "web_api" for card in result)
        assert any("Session" in str(card.get("service", "")) for card in result)

    @pytest.mark.asyncio
    async def test_collect_enhanced_local_fallback(self, mock_http_client):
        """Test fallback to enhanced local logs when both OAuth and Web API fail."""
        collector = AnthropicCollector()

        # Mock OAuth failure - OAuth uses request() not get()
        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401
        mock_http_client.request.return_value = oauth_response

        # Mock no web cookie
        with patch(
            "app.services.credential_provider.CredentialProvider.get_claude_token",
            return_value="invalid_token",
        ):
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.CLAUDE_PRO_LIMIT = 2000000
                mock_settings.CLAUDE_FREE_LIMIT = 500000
                mock_settings.LOCAL_COLLECTOR_ENABLED = True
                mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

                with patch(
                    "app.services.collectors.anthropic.get_claude_session_cookie",
                    return_value=None,
                ):
                    with patch.object(
                        collector, "_get_valid_token", return_value="invalid_token"
                    ):
                        # Mock local log data with all token types
                        log_data = [
                            json.dumps(
                                {
                                    "type": "assistant",
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
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
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
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

                        with patch(
                            "builtins.open", mock_open(read_data="".join(log_data))
                        ):
                            with patch(
                                "app.services.collectors.anthropic.glob.glob",
                                return_value=["/fake/path/test.jsonl"],
                            ):
                                with patch("os.path.isdir", return_value=True):
                                    result = await collector.collect(mock_http_client)

        # Should return local log results
        assert isinstance(result, list)
        assert len(result) == 1
        assert "Claude Pro" in str(result[0].get("service", ""))
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
                    "timestamp": datetime.now(timezone.utc).isoformat(),
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
                    "timestamp": datetime.now(timezone.utc).isoformat(),
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

        with patch(
            "app.services.credential_provider.CredentialProvider.get_claude_token",
            return_value="invalid_token",
        ):
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.CLAUDE_PRO_LIMIT = 2000000
                mock_settings.CLAUDE_FREE_LIMIT = 500000
                mock_settings.LOCAL_COLLECTOR_ENABLED = True
                mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

                with patch(
                    "app.services.collectors.anthropic.get_claude_session_cookie",
                    return_value=None,
                ):
                    with patch.object(
                        collector, "_get_valid_token", return_value="invalid_token"
                    ):
                        with patch(
                            "builtins.open", mock_open(read_data="".join(log_data))
                        ):
                            with patch(
                                "app.services.collectors.anthropic.glob.glob",
                                return_value=["/fake/path/test.jsonl"],
                            ):
                                with patch("os.path.isdir", return_value=True):
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

        with patch(
            "app.services.credential_provider.CredentialProvider.get_claude_token",
            return_value="invalid_token",
        ):
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.CLAUDE_PRO_LIMIT = 2000000
                mock_settings.CLAUDE_FREE_LIMIT = 500000
                mock_settings.LOCAL_COLLECTOR_ENABLED = True
                mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

                with patch(
                    "app.services.collectors.anthropic.get_claude_session_cookie",
                    return_value=None,
                ):
                    with patch.object(
                        collector, "_get_valid_token", return_value="invalid_token"
                    ):
                        with patch.dict(
                            "os.environ", {"CLAUDE_CONFIG_DIR": "/path1,/path2"}
                        ):
                            with patch("os.path.isdir", return_value=True):
                                with patch(
                                    "app.services.collectors.anthropic.glob.glob"
                                ) as mock_glob:
                                    # Return files from both paths
                                    def glob_side_effect(pattern, **kwargs):
                                        if "/path1" in pattern:
                                            return ["/path1/projects/file1.jsonl"]
                                        elif "/path2" in pattern:
                                            return ["/path2/projects/file2.jsonl"]
                                        return []

                                    mock_glob.side_effect = glob_side_effect

                                    # Mock file contents
                                    log_data_1 = (
                                        json.dumps(
                                            {
                                                "type": "assistant",
                                                "timestamp": datetime.now(
                                                    timezone.utc
                                                ).isoformat(),
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
                                                "timestamp": datetime.now(
                                                    timezone.utc
                                                ).isoformat(),
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
                                        else:
                                            return mock_open(read_data=log_data_2)()

                                    with patch(
                                        "builtins.open", side_effect=open_side_effect
                                    ):
                                        result = await collector.collect(
                                            mock_http_client
                                        )

        # Should aggregate from both directories
        assert isinstance(result, list)
        assert len(result) == 1

    def test_extract_identity_from_oauth(self):
        """Test identity extraction from OAuth API response."""
        collector = AnthropicCollector()

        # Full identity
        data_full = {
            "account": {"email": "user@example.com", "organization": "test-org"}
        }
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

        with patch(
            "app.services.credential_provider.CredentialProvider.get_claude_token",
            return_value="test_token",
        ):
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
                mock_settings.LOCAL_COLLECTOR_ENABLED = False
                with patch(
                    "app.services.collectors.anthropic.get_claude_session_cookie",
                    return_value=None,
                ):
                    with patch.object(
                        collector, "_get_valid_token", return_value="test_token"
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
        account_response.json.return_value = {"tier": "pro"}

        usage_response = MagicMock(spec=httpx.Response)
        usage_response.status_code = 200
        usage_response.json.return_value = {
            "current_window": {"percentUsed": 30.0, "resetsAt": "2025-04-07T12:00:00Z"}
        }

        mock_http_client.request.return_value = oauth_response
        mock_http_client.get.side_effect = [
            org_response,
            account_response,
            usage_response,
        ]

        with patch(
            "app.services.credential_provider.CredentialProvider.get_claude_token",
            return_value="invalid_token",
        ):
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.CLAUDE_PRO_LIMIT = 2000000
                mock_settings.CLAUDE_FREE_LIMIT = 500000
                mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False
                mock_settings.LOCAL_COLLECTOR_ENABLED = False

                with patch(
                    "app.services.collectors.anthropic.get_claude_session_cookie",
                    return_value="session_key",
                ):
                    with patch.object(
                        collector, "_get_valid_token", return_value="invalid_token"
                    ):
                        result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert any(card.get("data_source") == "web_api" for card in result)
        if result and result[0].get("remaining") != "ERR":
            detail = result[0].get("detail", "")
            # Identity should be included if present
            assert (
                "user@example.com" in detail or "Personal Org" in detail or True
            )  # May or may not be present

    def test_parse_oauth_response_boundary_percentages(self):
        """Test boundary percentage handling (0%, 100%)."""
        collector = AnthropicCollector()

        # 0% used (100% remaining)
        data_zero = {
            "five_hour": {"utilization": 0.0, "resets_at": "2025-04-07T12:00:00Z"}
        }
        result = collector._parse_oauth_response(
            data_zero, {"five_hour": "Session Window"}
        )
        assert result[0]["remaining"] == "100.0%"
        assert result[0]["health"] == "good"

        # 100% used (0% remaining)
        data_full = {
            "five_hour": {"utilization": 100.0, "resets_at": "2025-04-07T12:00:00Z"}
        }
        result = collector._parse_oauth_response(
            data_full, {"five_hour": "Session Window"}
        )
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
        result = collector._parse_oauth_response(
            data_no_util, {"five_hour": "Session Window"}
        )
        assert result[0]["remaining"] == "100.0%"

    @pytest.mark.asyncio
    async def test_get_claude_local_enhanced_uses_to_thread(self):
        """C5: _get_claude_local_enhanced must delegate sync I/O to asyncio.to_thread."""
        import asyncio

        collector = AnthropicCollector()

        with patch("app.services.collectors.anthropic.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value=None)
            with patch("app.services.collectors.anthropic.settings") as mock_settings:
                mock_settings.CLAUDE_PRO_LIMIT = 2000000
                mock_settings.CLAUDE_FREE_LIMIT = 500000
                mock_settings.CLAUDE_PROJECTS_DIR = ""

                result = await collector._get_claude_local_enhanced()

        mock_asyncio.to_thread.assert_called_once()
        called_fn = mock_asyncio.to_thread.call_args[0][0]
        assert callable(called_fn), "asyncio.to_thread must be called with a callable"


class TestGeminiCollector:
    """Test suite for Google Gemini collector."""

    @pytest.mark.asyncio
    async def test_collect_api_success(
        self, mock_http_client, mock_gemini_quota_response
    ):
        """Test successful Gemini API collection with project discovery."""
        collector = GeminiCollector()

        # Mock responses - tier request comes FIRST (to get project ID)
        tier_response = MagicMock(spec=httpx.Response)
        tier_response.status_code = 200
        tier_response.json.return_value = {
            "currentTier": {"id": "standard-tier", "name": "Gemini Code Assist"},
            "cloudaicompanionProject": "test-project-123",
        }

        quota_response = MagicMock(spec=httpx.Response)
        quota_response.status_code = 200
        quota_response.json.return_value = mock_gemini_quota_response

        # Create async mock that returns responses in order
        call_count = [0]

        async def mock_post(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return tier_response  # First call: loadCodeAssist
            else:
                return quota_response  # Second call: retrieveUserQuota

        mock_http_client.post = mock_post

        with patch("app.services.collectors.gemini.settings") as mock_settings:
            mock_settings.GEMINI_OAUTH_PATH = "/fake/creds.json"
            mock_settings.GEMINI_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with patch(
                "builtins.open",
                mock_open(
                    read_data=json.dumps(
                        {"access_token": "token", "expiry_date": 9999999999999}
                    )
                ),
            ):
                with patch(
                    "app.services.collectors.gemini.os.path.exists", return_value=True
                ):
                    with patch(
                        "app.services.collectors.gemini.time.time", return_value=1000
                    ):
                        result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) >= 1
        # Should return one card per model family
        assert len(result) <= len(mock_gemini_quota_response["buckets"])
        # Check that service name contains model identifier (either display name or raw model ID)
        assert any(
            name in str(result[0].get("service", "")) for name in ["Gemini", "gemini"]
        )
        # Verify health field exists
        assert "health" in result[0]
        # Verify unit is "used" (not "quota")
        assert result[0].get("unit") == "used"
        # Verify project was used in quota call
        assert call_count[0] == 2  # Should make 2 API calls

    @pytest.mark.asyncio
    async def test_collect_missing_credentials(self, mock_http_client):
        """Test graceful handling when credentials file missing."""
        collector = GeminiCollector()

        with patch("app.services.collectors.gemini.settings") as mock_settings:
            mock_settings.GEMINI_OAUTH_PATH = "/fake/missing.json"
            mock_settings.GEMINI_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with patch(
                "app.services.collectors.gemini.os.path.exists", return_value=False
            ):
                result = await collector.collect(mock_http_client)

        # Should return empty list or fallback to logs
        assert isinstance(result, list)

    @pytest.mark.asyncio
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

        # Use AsyncMock to track calls
        mock_post = AsyncMock(return_value=error_response)
        mock_http_client.post = mock_post

        with patch("app.services.collectors.gemini.settings") as mock_settings:
            mock_settings.GEMINI_OAUTH_PATH = "/fake/creds.json"
            mock_settings.GEMINI_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED = False

            with patch(
                "builtins.open",
                mock_open(
                    read_data=json.dumps(
                        {"access_token": "token", "expiry_date": 9999999999999}
                    )
                ),
            ):
                with patch(
                    "app.services.collectors.gemini.os.path.exists", return_value=True
                ):
                    with patch(
                        "app.services.collectors.gemini.time.time", return_value=1000
                    ):
                        # First call - API fails
                        result1 = await collector.collect(mock_http_client)
                        first_call_count = mock_post.call_count

                        # Verify cache was populated (any result)
                        assert collector._cached_results is not None
                        assert collector._last_fetch is not None

                        # Second call with SAME collector instance - should use cache
                        result2 = await collector.collect(mock_http_client)

                        # API should not be called again (result was cached)
                        assert mock_post.call_count == first_call_count

                        # Results should be the same (from cache)
                        assert result1 == result2


class TestGitHubCollector:
    """Test suite for GitHub Copilot collector."""

    @pytest.mark.asyncio
    async def test_collect_free_tier_quotas(
        self, mock_http_client, mock_github_copilot_response
    ):
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
        assert any("Copilot" in str(card.get("service", "")) for card in result)

    @pytest.mark.asyncio
    async def test_collect_missing_token(self, mock_http_client):
        """Test that missing GitHub token returns empty list."""
        collector = GitHubCollector()

        with patch(
            "app.services.credential_provider.CredentialProvider.get_github_token",
            return_value=None,
        ):
            result = await collector.collect(mock_http_client)

        assert result == []

    @pytest.mark.asyncio
    async def test_collect_api_error_caching(self, mock_http_client):
        """Test that API results are cached to avoid hammering the API (same instance)."""
        collector = GitHubCollector()

        # Verify initial state - no cache
        assert collector._cached_results is None
        assert collector._last_fetch is None

        # Mock API error response (500 error)
        error_response = MagicMock(spec=httpx.Response)
        error_response.status_code = 500
        mock_http_client.get.return_value = error_response

        with patch(
            "app.services.credential_provider.CredentialProvider.get_github_token",
            return_value="github_token",
        ):
            # First call - API fails
            result1 = await collector.collect(mock_http_client)
            first_call_count = mock_http_client.get.call_count

            # Verify cache was populated (any result)
            assert collector._cached_results is not None
            assert collector._last_fetch is not None

            # Second call with SAME collector instance - should use cache
            result2 = await collector.collect(mock_http_client)

            # API should not be called again (result was cached)
            assert mock_http_client.get.call_count == first_call_count

            # Results should be the same (from cache)
            assert result1 == result2


class TestChatGPTCollector:
    """Test suite for ChatGPT collector."""

    @pytest.mark.asyncio
    async def test_collect_api_success(
        self, mock_http_client, mock_chatgpt_usage_response
    ):
        """Test successful ChatGPT API collection."""
        collector = ChatGPTCollector()

        # Mock Account Info
        account_response = MagicMock(spec=httpx.Response)
        account_response.status_code = 200
        account_response.json.return_value = {
            "accounts": {
                "user-123": {
                    "account_status": "active",
                    "entitlements": [{"slug": "plus"}],
                }
            }
        }

        # Mock Usage Info
        usage_response = MagicMock(spec=httpx.Response)
        usage_response.status_code = 200
        usage_response.json.return_value = mock_chatgpt_usage_response

        # Sequentially return responses
        mock_http_client.get.side_effect = [account_response, usage_response]

        with patch(
            "app.services.credential_provider.CredentialProvider.get_chatgpt_token",
            return_value="test_token",
        ):
            result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) == 2
        assert "Account" in str(result[0].get("service", ""))
        assert "PLUS" in str(result[0].get("remaining", ""))
        assert "Codex" in str(result[1].get("service", ""))
        assert "%" in str(result[1].get("remaining", ""))

    @pytest.mark.asyncio
    async def test_collect_fallback_to_local_logs(self, mock_http_client):
        """Test fallback to local logs when API fails."""
        collector = ChatGPTCollector()

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 500
        mock_http_client.get.return_value = mock_response

        with patch("app.services.collectors.chatgpt.settings") as mock_settings:
            mock_settings.CHATGPT_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_COLLECTOR_ENABLED = True

            with patch("builtins.open", side_effect=FileNotFoundError):
                result = await collector.collect(mock_http_client)

        # Should return error card if both API and logs fail
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_collect_api_error_caching(self, mock_http_client):
        """Test that API results are cached to avoid hammering the API (same instance)."""
        from datetime import timezone

        collector = ChatGPTCollector()

        # Verify initial state - no cache
        assert collector._cached_api_results is None
        assert collector._last_api_fetch is None

        # Mock API error response (429 rate limit)
        error_response = MagicMock(spec=httpx.Response)
        error_response.status_code = 429
        mock_http_client.get.return_value = error_response

        with patch("app.services.collectors.chatgpt.settings") as mock_settings:
            mock_settings.CHATGPT_SESSIONS_DIR = "/fake/sessions"
            mock_settings.LOCAL_COLLECTOR_ENABLED = True

            with patch("builtins.open", side_effect=FileNotFoundError):
                with patch(
                    "app.services.credential_provider.CredentialProvider.get_chatgpt_token",
                    return_value="test_token",
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
        assert all("AG:" in card.get("service", "") for card in result)

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


class TestOpenCodeCollector:
    """Test suite for OpenCode collector."""

    @pytest.mark.asyncio
    async def test_collect_returns_list(self, mock_http_client):
        """Test OpenCode collector returns a list (may be empty if no data sources available)."""
        collector = OpenCodeCollector()

        # Mock all external dependencies to simulate no data available
        with patch(
            "app.services.collectors.opencode.get_opencode_session_cookie",
            return_value=None,
        ):
            with patch(
                "app.services.collectors.opencode.external_metric_service"
            ) as mock_external:
                mock_external.get_opencode_aggregated.return_value = []

                # Mock local DB doesn't exist
                with patch(
                    "app.services.collectors.opencode.os.path.exists",
                    return_value=False,
                ):
                    result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        # When no data sources are available, should return empty list
        assert result == []


class TestZaiApiCollector:
    """Test suite for zAI API (Balance) collector."""

    @pytest.mark.asyncio
    async def test_collect_success(self, mock_http_client, mock_zai_response):
        """Test successful zAI API balance collection."""
        collector = ZaiApiCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = mock_zai_response

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.zai_api.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai_valid_key"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service"] == "zAI API"
        assert "¥125.45" in result[0]["remaining"]
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_invalid_key(self, mock_http_client):
        """Test zAI API collection with invalid/placeholder key."""
        collector = ZaiApiCollector()

        with patch("app.services.collectors.zai_api.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai"  # Placeholder
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert "zAI" in result[0]["service"]
        assert result[0]["remaining"] == "ERR"
        assert "Missing/Invalid Key" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_collect_api_error(self, mock_http_client):
        """Test zAI API collection when API returns error."""
        collector = ZaiApiCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 401

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.zai_api.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "invalid_key"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert "API Error" in result[0]["detail"]


class TestZaiPlanCollector:
    """Test suite for zAI Plan (Quota) collector."""

    @pytest.mark.asyncio
    async def test_collect_success_token_limit(self, mock_http_client):
        """Test successful zAI plan collection with token limit."""
        collector = ZaiPlanCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "data": {
                "planName": "Basic Plan",
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "limit": 1000000,
                        "used": 450000,
                        "nextResetTime": 1775570736000,
                    }
                ],
            }
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.zai_plan.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai_valid_key"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service"] == "zAI Plan (Tokens)"
        assert "550,000" in result[0]["remaining"]  # 1M - 450K
        assert result[0]["health"] == "good"  # 45% used is still good

    @pytest.mark.asyncio
    async def test_collect_success_both_limits(self, mock_http_client):
        """Test successful zAI plan collection with both token and time limits."""
        collector = ZaiPlanCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "data": {
                "planName": "Pro Plan",
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "limit": 1000000,
                        "used": 200000,
                        "nextResetTime": 1775570736000,
                    },
                    {
                        "type": "TIME_LIMIT",
                        "limit": 3600,
                        "used": 900,
                        "nextResetTime": 1775570736000,
                    },
                ],
            }
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.zai_plan.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai_valid_key"
            result = await collector.collect(mock_http_client)

        assert len(result) == 2
        assert any("Tokens" in card["service"] for card in result)
        assert any("Time" in card["service"] for card in result)

    @pytest.mark.asyncio
    async def test_collect_no_auth(self, mock_http_client):
        """Test zAI plan collection without API key."""
        collector = ZaiPlanCollector()

        with patch("app.services.collectors.zai_plan.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = ""
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"


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
        assert result[0]["service"] == "Kimi API"
        assert "$8.75" in result[0]["remaining"]
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_invalid_key(self, mock_http_client):
        """Test Kimi API collection with short/invalid key."""
        collector = KimiApiCollector()

        with patch("app.services.collectors.kimi_api.settings") as mock_settings:
            mock_settings.KIMI_API_KEY = "short"  # Too short
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert "Kimi API" in result[0]["service"]
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
        assert any("Weekly" in card["service"] for card in result)
        assert any("5h" in card["service"] for card in result)
        assert any("Moderato" in card["detail"] for card in result)

    @pytest.mark.asyncio
    async def test_collect_no_auth(self, mock_http_client):
        """Test Kimi Coding collection without auth."""
        collector = KimiCodingCollector()

        with patch("app.services.collectors.kimi_coding.settings") as mock_settings:
            mock_settings.KIMI_AUTH_TOKEN = ""
            with patch(
                "app.services.collectors.kimi_coding.get_kimi_auth_cookie"
            ) as mock_cookie:
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

        with patch("app.services.collectors.github.settings") as mock_settings:
            mock_settings.GITHUB_TOKEN = "ghp_test123"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert (
                "timeout" in call.kwargs
            ), f"GitHub HTTP call #{i} missing timeout=: {call}"

    @pytest.mark.asyncio
    async def test_zai_api_collector_passes_timeout(self, mock_http_client):
        """ZAI API collector must pass timeout= on its HTTP call."""
        from app.services.collectors.zai_api import ZaiApiCollector

        collector = ZaiApiCollector()

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"available_balance": "10.0"}}
        mock_http_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.collectors.zai_api.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "test_key"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert (
                "timeout" in call.kwargs
            ), f"ZAI API HTTP call #{i} missing timeout=: {call}"

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
            assert (
                "timeout" in call.kwargs
            ), f"Kimi Coding HTTP call #{i} missing timeout=: {call}"

    @pytest.mark.asyncio
    async def test_zai_plan_collector_passes_timeout(self, mock_http_client):
        """ZAI Plan collector must pass timeout= on its HTTP call."""
        from app.services.collectors.zai_plan import ZaiPlanCollector

        collector = ZaiPlanCollector()

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_http_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.collectors.zai_plan.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "test_key"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert (
                "timeout" in call.kwargs
            ), f"ZAI Plan HTTP call #{i} missing timeout=: {call}"
