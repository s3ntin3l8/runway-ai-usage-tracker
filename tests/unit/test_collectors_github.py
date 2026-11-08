"""Unit tests for GitHub Copilot quota collector."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.collectors.github import GitHubCollector


class TestGitHubCollector:
    """Test suite for GitHub Copilot collector."""

    @staticmethod
    def _resp(status: int, data) -> MagicMock:
        r = MagicMock(spec=httpx.Response)
        r.status_code = status
        r.json.return_value = data
        r.headers = {}
        return r

    async def _collect_with_user_payload(self, mock_http_client, payload) -> list[dict]:
        """Run one collection where copilot_internal/user returns ``payload``."""
        collector = GitHubCollector(account_id="s3ntin3l8", account_label="s3ntin3l8")
        request = AsyncMock(side_effect=[self._resp(200, payload)])
        with (
            patch.object(collector, "_get_token", new_callable=AsyncMock, return_value="gho_x"),
            patch("app.services.collectors.github.http_request_with_retry", request),
        ):
            cards = await collector._strategy_api(mock_http_client)
        # One call only: the v2/token fallback is gone.
        assert request.await_count == 1
        return cards

    @pytest.mark.asyncio
    async def test_collect_free_tier_quotas(self, mock_http_client):
        """Free-tier cards carry the real totals from monthly_quotas."""
        cards = await self._collect_with_user_payload(
            mock_http_client,
            {
                "limited_user_quotas": {"completions": 1500, "chat": 40},
                "monthly_quotas": {"completions": 2000, "chat": 50},
                "limited_user_reset_date": "2026-11-01",
                "copilot_plan": "free",
            },
        )

        by_variant = {c["variant"]: c for c in cards}
        assert by_variant["Completions"]["remaining"] == "1,500"
        assert by_variant["Completions"]["unit"] == "/ 2,000"
        assert by_variant["Completions"]["used_value"] == 500.0
        assert by_variant["Chat"]["limit_value"] == 50.0
        assert by_variant["Chat"]["reset_at"].startswith("2026-11-01")

    @pytest.mark.asyncio
    async def test_free_tier_without_totals_does_not_invent_a_limit(self, mock_http_client):
        cards = await self._collect_with_user_payload(
            mock_http_client, {"limited_user_quotas": {"chat": 40}}
        )

        (card,) = cards
        assert card["remaining"] == "40"
        assert card["unit"] == "remaining"
        assert "??" in card["detail"]
        # Nothing for the chart/pct derivation to divide by.
        assert card["limit_value"] is None
        assert card["used_value"] is None

    @pytest.mark.asyncio
    async def test_a_float_total_is_a_known_total_in_every_field(self, mock_http_client):
        """Display text and numeric fields agree whether the total arrives as 50 or 50.0."""
        cards = await self._collect_with_user_payload(
            mock_http_client,
            {"limited_user_quotas": {"chat": 40}, "monthly_quotas": {"chat": 50.0}},
        )

        (card,) = cards
        assert card["unit"] == "/ 50"
        assert card["detail"].startswith("40/50 requests left")
        assert (card["used_value"], card["limit_value"]) == (10.0, 50.0)

    @pytest.mark.asyncio
    async def test_free_tier_accepts_an_epoch_reset_date(self, mock_http_client):
        cards = await self._collect_with_user_payload(
            mock_http_client,
            {
                "limited_user_quotas": {"chat": 40},
                "monthly_quotas": {"chat": 50},
                "limited_user_reset_date": 1793491200,
            },
        )

        assert cards[0]["reset_at"].startswith("2026-11-01")

    @pytest.mark.asyncio
    async def test_null_limited_quotas_for_paid_plans_do_not_crash(self, mock_http_client):
        cards = await self._collect_with_user_payload(
            mock_http_client,
            {
                "limited_user_quotas": None,
                "quota_snapshots": {
                    "chat": {"remaining": 890, "entitlement": 1000},
                },
                "copilot_plan": "pro",
            },
        )

        assert cards
        assert all(card["remaining"] != "ERR" for card in cards)

    @pytest.mark.asyncio
    async def test_a_rejected_token_makes_no_second_request(self, mock_http_client):
        collector = GitHubCollector(account_id="s3ntin3l8", account_label="s3ntin3l8")
        request = AsyncMock(side_effect=[self._resp(401, {})])
        with (
            patch.object(collector, "_get_token", new_callable=AsyncMock, return_value="gho_x"),
            patch("app.services.collectors.github.http_request_with_retry", request),
        ):
            cards = await collector._strategy_api(mock_http_client)

        assert request.await_count == 1
        assert cards == []

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
        assert collector.last_retry_after == 60.0

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

    @pytest.mark.asyncio
    async def test_account_id_resolved_to_login_when_no_email(self, mock_http_client):
        """account_id and label are set to the GitHub login even when a display name exists.

        Simulates the device-flow case: token has no email scope, /user profile has
        no email (private) and /user/emails returns 403 / empty. The login wins over
        the display name so both account_id and account_label are stable and consistent.
        """
        collector = GitHubCollector()

        def _resp(status: int, data) -> MagicMock:
            r = MagicMock(spec=httpx.Response)
            r.status_code = status
            r.json.return_value = data
            r.headers = {}
            return r

        copilot_user = _resp(
            200,
            {
                "quota_snapshots": [
                    {"metric": "chat", "used": 15, "included": 200, "quota_reset_at": 0}
                ],
                "copilot_plan": "Individual",
                "quota_reset_date_utc": "2026-07-01T00:00:00.000Z",
            },
        )
        # Realistic response: name present but email private — login must win over name.
        std_user = _resp(200, {"login": "s3ntin3l8", "email": None, "name": "Björn Hansen"})
        emails_list = _resp(200, [])

        # Simulate git config returning no email (non-zero exit)
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with (
            patch.object(
                collector, "_get_token", new_callable=AsyncMock, return_value="ghp_testtoken"
            ),
            patch(
                "app.services.collectors.github.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[copilot_user, std_user, emails_list],
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc),
        ):
            await collector._strategy_api(mock_http_client)

        # login beats display name — both id and label use the login
        assert collector.account_id == "s3ntin3l8"
        assert collector.account_label == "s3ntin3l8"

    @pytest.mark.asyncio
    async def test_account_id_is_login_even_when_email_available(self, mock_http_client):
        """account_id is the GitHub login; an exposed email is only the display label.

        Keying by email split one account into a login series and an email series
        (Data Health "no supporting evidence"), so the login always wins.
        """
        collector = GitHubCollector()

        def _resp(status: int, data) -> MagicMock:
            r = MagicMock(spec=httpx.Response)
            r.status_code = status
            r.json.return_value = data
            r.headers = {}
            return r

        copilot_user = _resp(
            200,
            {
                "quota_snapshots": [
                    {"metric": "chat", "used": 15, "included": 200, "quota_reset_at": 0}
                ],
                "copilot_plan": "Individual",
                "quota_reset_date_utc": "2026-07-01T00:00:00.000Z",
            },
        )
        std_user = _resp(
            200, {"login": "s3ntin3l8", "email": "bjoern@example.com", "name": "Björn Hansen"}
        )

        with (
            patch.object(
                collector, "_get_token", new_callable=AsyncMock, return_value="ghp_testtoken"
            ),
            patch(
                "app.services.collectors.github.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[copilot_user, std_user],
            ),
        ):
            await collector._strategy_api(mock_http_client)

        assert collector.account_id == "s3ntin3l8"
        assert collector.account_label == "bjoern@example.com"

    @pytest.mark.asyncio
    async def test_server_git_email_is_never_used_as_identity(self, mock_http_client):
        """The server host's `git config user.email` must not become the account identity."""
        collector = GitHubCollector()

        def _resp(status: int, data) -> MagicMock:
            r = MagicMock(spec=httpx.Response)
            r.status_code = status
            r.json.return_value = data
            r.headers = {}
            return r

        copilot_user = _resp(
            200,
            {
                "quota_snapshots": [
                    {"metric": "chat", "used": 1, "included": 200, "quota_reset_at": 0}
                ],
                "copilot_plan": "Individual",
            },
        )
        std_user = _resp(200, {"login": "s3ntin3l8", "email": None})
        emails_list = _resp(200, [])

        with (
            patch.object(
                collector, "_get_token", new_callable=AsyncMock, return_value="ghp_testtoken"
            ),
            patch(
                "app.services.collectors.github.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[copilot_user, std_user, emails_list],
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            await collector._strategy_api(mock_http_client)

        mock_exec.assert_not_awaited()
        assert collector.account_id == "s3ntin3l8"
        assert collector.account_label == "s3ntin3l8"

    @pytest.mark.asyncio
    async def test_default_collector_ignores_leaked_email_label(self, mock_http_client):
        """Default collector with a leaked email account_label does NOT short-circuit identity.

        If account_label was populated with an email (e.g. from token_cache metadata
        leaked across providers), a default collector must still hit the /user API
        instead of falsely claiming the email as its identity.
        """
        collector = GitHubCollector()
        collector.account_label = "leaked@example.com"

        def _resp(status: int, data) -> MagicMock:
            r = MagicMock(spec=httpx.Response)
            r.status_code = status
            r.json.return_value = data
            r.headers = {}
            return r

        copilot_user = _resp(
            200,
            {
                "quota_snapshots": [
                    {"metric": "chat", "used": 5, "included": 200, "quota_reset_at": 0}
                ],
                "copilot_plan": "Individual",
            },
        )
        std_user = _resp(200, {"login": "real_github_login", "email": None})
        emails_list = _resp(200, [])

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with (
            patch.object(
                collector, "_get_token", new_callable=AsyncMock, return_value="ghp_testtoken"
            ),
            patch(
                "app.services.collectors.github.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[copilot_user, std_user, emails_list],
            ) as mock_req,
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc),
        ):
            await collector._strategy_api(mock_http_client)

        # /user was indeed called (side_effect had std_user and emails_list)
        assert mock_req.await_count >= 2
        # account_id is pinned to real_github_login instead of leaked@example.com
        assert collector.account_id == "real_github_login"
        # leaked email label is also discarded and replaced with real_github_login
        assert collector.account_label == "real_github_login"

    @pytest.mark.asyncio
    async def test_scoped_collector_uses_matching_email_label(self, mock_http_client):
        """Scoped collector whose account_id matches the email label skips /user lookup."""
        collector = GitHubCollector(account_id="pinned@example.com")
        collector.account_label = "pinned@example.com"

        def _resp(status: int, data) -> MagicMock:
            r = MagicMock(spec=httpx.Response)
            r.status_code = status
            r.json.return_value = data
            r.headers = {}
            return r

        copilot_user = _resp(
            200,
            {
                "quota_snapshots": [
                    {"metric": "chat", "used": 5, "included": 200, "quota_reset_at": 0}
                ],
                "copilot_plan": "Individual",
            },
        )

        with (
            patch.object(
                collector, "_get_token", new_callable=AsyncMock, return_value="ghp_testtoken"
            ),
            patch(
                "app.services.collectors.github.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=copilot_user,
            ) as mock_req,
        ):
            await collector._strategy_api(mock_http_client)

        # Only copilot internal API called, /user skipped because identity was already shortcutted
        assert mock_req.await_count == 1
        assert collector.account_id == "pinned@example.com"
