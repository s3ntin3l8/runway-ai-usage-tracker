"""Unit tests for Anthropic (Claude) quota collector."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import httpx
import pytest

from app.services.collectors.anthropic import AnthropicCollector
from tests.fixtures.mock_data import ANTHROPIC_OAUTH_LIMITS_RESPONSE


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
        # Only the aggregate weekly window remains (Anthropic retired seven_day_sonnet);
        # it carries no model_id.
        weekly_card = next(
            (c for c in result if c.get("window_type") == "weekly"),
            None,
        )
        assert weekly_card is not None, "Expected an aggregate Weekly card from mock data"
        assert weekly_card.get("model_id") is None
        # No per-model Sonnet weekly card should be produced anymore.
        assert not any(
            c.get("window_type") == "weekly" and c.get("model_id") == "sonnet" for c in result
        )

    @pytest.mark.asyncio
    async def test_refresh_token_lookup_is_scoped_to_account(self, mock_http_client):
        """Test that Anthropic refresh token fallback uses the collector account."""
        collector = AnthropicCollector(account_id="acc_a")

        with (
            patch(
                "app.services.collectors.anthropic_oauth.token_cache.get_token",
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
            assert collector.last_retry_after == 300.0

            # Second direct call - still returns 429 (no proactive backoff)
            result2 = await collector._get_claude_oauth(mock_http_client, "test_token")
            assert result2[0].get("error_type") == "rate_limited"

    @pytest.mark.asyncio
    async def test_usage_429_never_spends_a_refresh_token(self, mock_http_client):
        """A usage-endpoint 429 is a rate limit, not an expired token: refresh tokens are
        single-use, so the collector must not force a refresh as a "remedy"."""
        collector = AnthropicCollector()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        resp.headers = {}
        mock_http_client.request.return_value = resp

        with patch.object(collector, "_get_valid_token") as get_token:
            result = await collector._get_claude_oauth(mock_http_client, "test_token")

        get_token.assert_not_called()
        assert result[0].get("error_type") == "rate_limited"

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

        # Should return 4 cards: 2 standard quota windows (Session, Weekly) + Balance + Extra Usage
        assert len(result) == 4

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
                    mock_settings.CLAUDE_PRO_LIMIT = 2000000
                    mock_settings.CLAUDE_FREE_LIMIT = 500000

                    # Return False for expiration check so we hit the reactive path (401 response)
                    with patch.object(collector, "is_token_expired", return_value=False):
                        # Mock refresh token availability (from the token cache)
                        with patch(
                            "app.services.collectors.anthropic_oauth.token_cache.get_token",
                            new_callable=AsyncMock,
                            return_value="valid_refresh_token",
                        ):
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

            with (
                patch(
                    "app.services.collectors.anthropic_web.get_claude_session_cookie",
                    return_value="sk-ant-session123",
                ),
                patch.object(collector, "_get_valid_token", return_value="invalid_token"),
            ):
                result = await collector.collect(mock_http_client)

        # Should return Web API results — 2 cards (session + aggregate weekly); the mock
        # data has no per-model seven_day windows.
        assert isinstance(result, list)
        assert len(result) == 2
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
    async def test_web_api_email_promotes_only_the_default_account_identity(self):
        collector = AnthropicCollector(account_id="default")
        update_identity = AsyncMock()
        with patch(
            "app.services.collectors.anthropic_web.token_cache.update_account_metadata",
            update_identity,
        ):
            collector._parse_web_api_response(
                {}, org_data={"membership": {"user": {"email": "User@Example.com"}}}
            )
            await asyncio.sleep(0)

        assert collector.account_id == "user@example.com"
        update_identity.assert_awaited_once_with(
            "anthropic", "user@example.com", name="User@Example.com"
        )

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
        assert len(result) == 2  # session + aggregate weekly in mock usage data
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
        result = collector.parse_oauth_response(data_zero, {"five_hour": "Session Window"})
        assert result[0]["remaining"] == "100.0%"
        assert result[0]["health"] == "good"

        # 100% used (0% remaining)
        data_full = {"five_hour": {"utilization": 100.0, "resets_at": "2025-04-07T12:00:00Z"}}
        result = collector.parse_oauth_response(data_full, {"five_hour": "Session Window"})
        assert result[0]["remaining"] == "0.0%"
        assert result[0]["health"] == "critical"

    def test_parse_oauth_response_invalid_timestamp(self):
        """Test graceful handling of invalid reset timestamps."""
        collector = AnthropicCollector()

        data = {"five_hour": {"utilization": 25.0, "resets_at": "invalid-timestamp"}}
        result = collector.parse_oauth_response(data, {"five_hour": "Session Window"})

        # Should not crash, should return card with reset as "—"
        # Returns 2 items: both core keys (Session, Weekly) get default cards when null
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["reset"] == "—"

    def test_parse_oauth_response_empty_windows(self):
        """Test handling when no valid quota windows present."""
        collector = AnthropicCollector()

        # Empty data - should default to 100% remaining for guaranteed windows
        result = collector.parse_oauth_response({}, {"five_hour": "Session Window"})
        assert result[0]["remaining"] == "100.0%"

        # Data without utilization field - should default to 100% remaining for guaranteed windows
        data_no_util = {"five_hour": {"resets_at": "2025-04-07T12:00:00Z"}}
        result = collector.parse_oauth_response(data_no_util, {"five_hour": "Session Window"})
        assert result[0]["remaining"] == "100.0%"

    def test_parse_oauth_response_limits_array(self):
        """Test the newer `limits[]` OAuth response shape (kind/group/percent/scope).

        Anthropic's usage response is now a HYBRID: the legacy dict-keyed-by-window
        (five_hour/seven_day/seven_day_opus/...) coexists with a new top-level
        `limits` array of self-describing objects carrying the SAME values. This
        must produce exactly a session card, an aggregate weekly card, and a
        per-model weekly card (Fable) — with no duplicate session/weekly cards from
        the legacy keys and no phantom cards from the disabled extra_usage/spend
        objects (both non-null dicts with neither a balance nor spend/limit shape).
        """
        collector = AnthropicCollector()
        name_map = {"five_hour": "Session", "seven_day": "Weekly"}

        result = collector.parse_oauth_response(ANTHROPIC_OAUTH_LIMITS_RESPONSE, name_map)

        assert len(result) == 3, (
            f"Expected exactly 3 cards, got {[c['window_type'] for c in result]}"
        )

        session_cards = [c for c in result if c["window_type"] == "session"]
        assert len(session_cards) == 1, "five_hour must not duplicate the limits[] session card"
        session_card = session_cards[0]
        assert session_card["used_value"] == 25
        assert session_card["model_id"] is None
        assert session_card["remaining"] == "75.0%"
        assert "[OAuth]" in session_card["detail"]

        weekly_cards = [c for c in result if c["window_type"] == "weekly"]
        assert len(weekly_cards) == 2, "seven_day must not duplicate the limits[] weekly_all card"
        assert not any(c.get("model_id") == "opus" for c in result)
        assert not any(c.get("model_id") == "sonnet" for c in result)

        weekly_all = next(c for c in weekly_cards if c["model_id"] is None)
        assert weekly_all["used_value"] == 89
        assert weekly_all["health"] == "warning"

        weekly_fable = next(c for c in weekly_cards if c["model_id"] == "fable")
        assert weekly_fable["used_value"] == 0
        assert weekly_fable["reset_at"] is None
        assert weekly_fable["reset"] == "—"

    def test_parse_web_api_response_limits_array(self):
        """Web scraper endpoint (claude.ai/api/organizations/{id}/usage) is the ACTIVE
        strategy for most users (first-success-wins dispatch) and returns the same
        hybrid `limits[]` shape confirmed live via debug/raw. Must produce the same
        three cards as OAuth, tagged as the web data source, with no duplicates.
        """
        collector = AnthropicCollector()

        result = collector._parse_web_api_response(ANTHROPIC_OAUTH_LIMITS_RESPONSE)

        assert len(result) == 3, (
            f"Expected exactly 3 cards, got {[c['window_type'] for c in result]}"
        )
        assert all(c["data_source"] == "web" for c in result)
        assert all("[Web API]" in c["detail"] for c in result)

        session_cards = [c for c in result if c["window_type"] == "session"]
        assert len(session_cards) == 1, "five_hour must not duplicate the limits[] session card"
        assert session_cards[0]["used_value"] == 25

        weekly_cards = [c for c in result if c["window_type"] == "weekly"]
        assert len(weekly_cards) == 2, "seven_day must not duplicate the limits[] weekly_all card"
        assert not any(c.get("model_id") == "opus" for c in result)
        assert not any(c.get("model_id") == "sonnet" for c in result)

        weekly_all = next(c for c in weekly_cards if c["model_id"] is None)
        assert weekly_all["used_value"] == 89
        assert weekly_all["health"] == "warning"

        weekly_fable = next(c for c in weekly_cards if c["model_id"] == "fable")
        assert weekly_fable["used_value"] == 0
        assert weekly_fable["reset_at"] is None
        assert weekly_fable["reset"] == "—"

    def test_parse_oauth_response_empty_limits_array_falls_back_to_legacy(self):
        """A present-but-empty `limits: []` (plausible during partial API rollout)
        must NOT be treated as authoritative — it should fall back to parsing the
        legacy dict-keyed-by-window-name keys, or the Claude tile would go empty
        even though five_hour/seven_day still carry real values.
        """
        collector = AnthropicCollector()
        name_map = {"five_hour": "Session", "seven_day": "Weekly"}
        data = {
            "five_hour": {"utilization": 25.0, "resets_at": "2026-07-20T21:30:00Z"},
            "seven_day": {"utilization": 89.0, "resets_at": "2026-07-21T18:00:00Z"},
            "limits": [],
        }

        result = collector.parse_oauth_response(data, name_map)

        assert len(result) == 2
        session_card = next(c for c in result if c["window_type"] == "session")
        assert session_card["used_value"] == 25.0
        weekly_card = next(c for c in result if c["window_type"] == "weekly")
        assert weekly_card["used_value"] == 89.0

    def test_parse_web_api_response_empty_limits_array_falls_back_to_legacy(self):
        """Same empty-array fallback guarantee as OAuth, for the web parser."""
        collector = AnthropicCollector()
        data = {
            "five_hour": {"utilization": 25.0, "resets_at": "2026-07-20T21:30:00Z"},
            "seven_day": {"utilization": 89.0, "resets_at": "2026-07-21T18:00:00Z"},
            "limits": [],
        }

        result = collector._parse_web_api_response(data)

        assert len(result) == 2
        session_card = next(c for c in result if c["window_type"] == "session")
        assert session_card["used_value"] == 25.0
        weekly_card = next(c for c in result if c["window_type"] == "weekly")
        assert weekly_card["used_value"] == 89.0

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="local strategy moved to sidecar")
    async def test_get_claude_local_enhanced_uses_to_thread(self):
        """C5: _get_claude_local_enhanced must delegate sync I/O to asyncio.to_thread."""

        collector = AnthropicCollector()

        with patch("app.services.collectors.anthropic_local.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value=None)
            await collector._get_claude_local_enhanced()

        mock_asyncio.to_thread.assert_called_once()
        called_fn = mock_asyncio.to_thread.call_args[0][0]
        assert callable(called_fn), "asyncio.to_thread must be called with a callable"

    def test_tier_comes_from_organization_info_then_the_usage_response(self):
        """The plan is inferred from the organization info, then the usage response.

        The server reads no credentials file, so there is no ``rateLimitTier`` fallback.
        """
        collector = AnthropicCollector()
        name_map = {"five_hour": "Session Window"}
        data = {"five_hour": {"utilization": 10.0}}

        result = collector.parse_oauth_response(data, name_map, {"tier": "Max"})
        assert result[0]["tier"] == "Max"

        data_with_plan = {"account": {"plan": "plus"}, "five_hour": {"utilization": 10.0}}
        result = collector.parse_oauth_response(data_with_plan, name_map, None)
        assert result[0]["tier"] == "Plus"

        # The org info outranks the usage response.
        result = collector.parse_oauth_response(data_with_plan, name_map, {"tier": "Team"})
        assert result[0]["tier"] == "Team"

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
