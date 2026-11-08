"""Unit tests for MiniMax quota collectors."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.utils import is_error_card_dict
from app.services.collectors.minimax import MiniMaxCollector


class TestMiniMaxCollector:
    """Test suite for MiniMax collector, against the real /v1/coding_plan/remains
    payload — the previous fixture here used a fabricated `remains` field that
    never existed in MiniMax's actual response, which is exactly how the
    original parsing bug shipped undetected."""

    # Real response captured from a live account with a fresh (unused)
    # subscription: both windows report 0/0 counts and 100% remaining. The
    # "video" entry has current_interval_status=3 (not entitled) alongside
    # "general"'s status=1 — video quota is a separate entitlement this
    # collector doesn't surface.
    REAL_PAYLOAD_UNUSED = {
        "model_remains": [
            {
                "start_time": 1788361200000,
                "end_time": 1788379200000,
                "remains_time": 4692260,
                "current_interval_total_count": 0,
                "current_interval_usage_count": 0,
                "model_name": "general",
                "current_weekly_total_count": 0,
                "current_weekly_usage_count": 0,
                "weekly_start_time": 1788134400000,
                "weekly_end_time": 1788739200000,
                "weekly_remains_time": 364692260,
                "current_interval_status": 1,
                "current_interval_remaining_percent": 100,
                "current_weekly_status": 1,
                "current_weekly_remaining_percent": 100,
            },
            {
                "start_time": 1788307200000,
                "end_time": 1788393600000,
                "remains_time": 19092260,
                "current_interval_total_count": 0,
                "current_interval_usage_count": 0,
                "model_name": "video",
                "current_weekly_total_count": 0,
                "current_weekly_usage_count": 0,
                "weekly_start_time": 1788134400000,
                "weekly_end_time": 1788739200000,
                "weekly_remains_time": 364692260,
                "current_interval_status": 3,
                "current_interval_remaining_percent": 100,
                "current_weekly_status": 3,
                "current_weekly_remaining_percent": 100,
            },
        ],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }

    @pytest.mark.asyncio
    async def test_collect_unused_plan(self, mock_http_client):
        """Fresh/unused subscription: two cards (session + weekly), no video card,
        pct_used explicitly 0.0 (not None), model_id None, reset_at from the real
        end_time/weekly_end_time fields."""
        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "mm_valid_key"
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = self.REAL_PAYLOAD_UNUSED
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert len(result) == 2
        by_window = {c["window_type"]: c for c in result}
        assert set(by_window) == {"session", "weekly"}

        session_card = by_window["session"]
        assert session_card["service_name"] == "MiniMax"
        assert session_card["model_id"] is None
        assert session_card["pct_used"] == 0.0
        assert session_card["reset_at"] == "2026-09-02T20:00:00+00:00"
        assert session_card["health"] == "good"

        weekly_card = by_window["weekly"]
        assert weekly_card["pct_used"] == 0.0
        assert weekly_card["reset_at"] == "2026-09-07T00:00:00+00:00"

        # No standalone "video" card, and no monthly window at all.
        assert all(c["window_type"] != "monthly" for c in result)
        assert not any("video" in c.get("detail", "") for c in result)

    @pytest.mark.asyncio
    async def test_collect_with_counts(self, mock_http_client):
        """Once the plan reports real counts, pct_used is derived from
        usage_count/total_count (not the *_remaining_percent field, which is
        remaining, not used)."""
        payload = {
            "model_remains": [
                {
                    "start_time": 1788361200000,
                    "end_time": 1788379200000,
                    "remains_time": 4692260,
                    "current_interval_total_count": 400,
                    "current_interval_usage_count": 40,
                    "model_name": "general",
                    "current_weekly_total_count": 4000,
                    "current_weekly_usage_count": 400,
                    "weekly_start_time": 1788134400000,
                    "weekly_end_time": 1788739200000,
                    "weekly_remains_time": 364692260,
                    "current_interval_status": 1,
                    "current_interval_remaining_percent": 90,
                    "current_weekly_status": 1,
                    "current_weekly_remaining_percent": 90,
                }
            ],
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "mm_valid_key"
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = payload
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert len(result) == 2
        by_window = {c["window_type"]: c for c in result}
        assert by_window["session"]["pct_used"] == 10.0
        assert by_window["session"]["used_value"] == 40.0
        assert by_window["session"]["limit_value"] == 400.0
        assert by_window["session"]["unit_type"] == "requests"
        assert by_window["weekly"]["pct_used"] == 10.0
        assert by_window["weekly"]["used_value"] == 400.0
        assert by_window["weekly"]["limit_value"] == 4000.0

    @pytest.mark.asyncio
    async def test_collect_no_entitled_window(self, mock_http_client):
        """No entry with current_interval_status in ENTITLED_STATUSES ({1, 2}) ->
        treated the same as an empty model_remains: a single 'No active plan'
        card. Uses the "video" bucket's status=3, which is the observed "not
        your bucket" marker distinct from the {1, 2} available/exhausted pair."""
        payload = {
            "model_remains": [
                {**self.REAL_PAYLOAD_UNUSED["model_remains"][1], "model_name": "video"}
            ],
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "mm_valid_key"
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = payload
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert "No active plan" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_collect_exhausted_session_still_shown(self, mock_http_client):
        """current_interval_status=2 (observed live: 0% remaining, an exhausted-
        but-owned window) must still produce a session card — this is the bug
        found in production: gating on status==1 alone hid the card at exactly
        the moment (0% remaining) a user most needs to see it."""
        payload = {
            "model_remains": [
                {
                    "start_time": 1788379200000,
                    "end_time": 1788393600000,
                    "remains_time": 7367406,
                    "current_interval_total_count": 0,
                    "current_interval_usage_count": 0,
                    "model_name": "general",
                    "current_weekly_total_count": 0,
                    "current_weekly_usage_count": 0,
                    "weekly_start_time": 1788134400000,
                    "weekly_end_time": 1788739200000,
                    "weekly_remains_time": 352967406,
                    "current_interval_status": 2,
                    "current_interval_remaining_percent": 0,
                    "current_weekly_status": 1,
                    "current_weekly_remaining_percent": 82,
                }
            ],
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "mm_valid_key"
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = payload
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert len(result) == 2
        by_window = {c["window_type"]: c for c in result}
        session = by_window["session"]
        assert session["pct_used"] == 100.0
        assert session["remaining"] == "0.0%"
        assert session["health"] == "critical"
        assert by_window["weekly"]["pct_used"] == 18.0

    @pytest.mark.asyncio
    async def test_session_entitled_but_weekly_not_omits_weekly_card(self, mock_http_client):
        """Session (current_interval_status=1) and weekly (current_weekly_status)
        entitlement are gated independently — a session-entitled, weekly-expired
        entry must produce only a session card, not a stale/zero weekly one built
        from unentitled weekly fields."""
        payload = {
            "model_remains": [
                {
                    **self.REAL_PAYLOAD_UNUSED["model_remains"][0],
                    "current_interval_status": 1,
                    "current_weekly_status": 3,
                }
            ],
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "mm_valid_key"
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = payload
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert [c["window_type"] for c in result] == ["session"]

    @pytest.mark.asyncio
    async def test_remaining_clamps_at_zero_on_overage(self, mock_http_client):
        """usage_count > total_count (overage) must not render a negative
        remaining percentage."""
        payload = {
            "model_remains": [
                {
                    **self.REAL_PAYLOAD_UNUSED["model_remains"][0],
                    "current_interval_total_count": 100,
                    "current_interval_usage_count": 150,
                    "current_weekly_total_count": 100,
                    "current_weekly_usage_count": 150,
                }
            ],
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "mm_valid_key"
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = payload
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        for card in result:
            assert card["pct_used"] == 150.0  # not itself clamped
            assert card["remaining"] == "0.0%"  # display clamped

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


class TestMiniMaxOAuthCollector:
    """The MiniMax Code app's OAuth token, and the open-platform endpoint it needs.

    The app's own quota client calls
    ``/v1/api/openplatform/coding_plan/remains`` on the open-platform host with
    the OAuth access token as a bearer. The API key and the OAuth token are
    not interchangeable: the API host answers ``status_code 1004 login fail``
    for an OAuth token, and the open-platform path answers it for an API key.
    """

    PAYLOAD = {
        "model_remains": [
            {
                "start_time": 1792508400000,
                "end_time": 1792526400000,
                "remains_time": 11586321,
                "current_interval_total_count": 0,
                "current_interval_usage_count": 0,
                "model_name": "general",
                "current_weekly_total_count": 0,
                "current_weekly_usage_count": 0,
                "weekly_start_time": 1792368000000,
                "weekly_end_time": 1792972800000,
                "current_interval_status": 1,
                "current_interval_remaining_percent": 100,
                "current_weekly_status": 1,
                "current_weekly_remaining_percent": 99,
            },
            {
                "model_name": "video",
                "current_interval_status": 3,
                "current_weekly_status": 3,
                "current_interval_remaining_percent": 100,
                "current_weekly_remaining_percent": 100,
            },
        ],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }

    @pytest.mark.asyncio
    async def test_oauth_token_uses_the_openplatform_endpoint(self, mock_http_client):
        """The OAuth token goes to platform.<host>/v1/api/openplatform/..."""
        with (
            patch("app.services.collectors.minimax.settings") as mock_settings,
            patch(
                "app.services.collectors.minimax.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.token_cache.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=(
                    {"oauth_token": "mmoat_test_token"},  # pragma: allowlist secret
                    {"source": "sidecar"},
                ),
            ),
        ):
            mock_settings.MINIMAX_API_KEY = ""
            mock_settings.MINIMAX_HOST = ""
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = self.PAYLOAD
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        url = mock_http_client.get.call_args[0][0]
        assert url == "https://platform.minimax.io/v1/api/openplatform/coding_plan/remains"
        headers = mock_http_client.get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer mmoat_test_token"
        # The payload is the same shape the API-key path returns, so the same
        # parser builds the same two cards (session + weekly; "video" excluded).
        by_window = {c["window_type"]: c for c in result}
        assert set(by_window) == {"session", "weekly"}
        assert by_window["session"]["detail"].endswith("[App]")

    @pytest.mark.asyncio
    async def test_api_key_still_uses_the_api_endpoint(self, mock_http_client):
        """An API key must keep using the api.<host> path it always used."""
        with (
            patch("app.services.collectors.minimax.settings") as mock_settings,
            patch(
                "app.services.collectors.minimax.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.token_cache.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=(
                    {"api_key": "sk_test_key"},  # pragma: allowlist secret
                    {"source": "sidecar"},
                ),
            ),
        ):
            mock_settings.MINIMAX_API_KEY = ""
            mock_settings.MINIMAX_HOST = ""
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = self.PAYLOAD
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        url = mock_http_client.get.call_args[0][0]
        assert url == "https://api.minimax.io/v1/coding_plan/remains"
        assert result[0]["detail"].endswith("[API]")

    def test_open_platform_host_is_region_aware(self):
        """The open-platform host differs by region: the global region serves it
        from platform.<host>, China from www.minimaxi.com."""
        collector = MiniMaxCollector()
        collector.host = "minimax.io"
        assert collector._open_platform_base_url() == "https://platform.minimax.io"
        collector.host = "minimaxi.com"
        assert collector._open_platform_base_url() == "https://www.minimaxi.com"

    @pytest.mark.asyncio
    async def test_api_key_wins_when_both_are_present(self, mock_http_client):
        """A bundle carrying both keeps using the API key — the app's OAuth token
        is a fallback, not a replacement for an existing API key."""
        with (
            patch("app.services.collectors.minimax.settings") as mock_settings,
            patch(
                "app.services.collectors.minimax.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.token_cache.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=(
                    {
                        "api_key": "sk_test_key",  # pragma: allowlist secret
                        "oauth_token": "mmoat_test_token",  # pragma: allowlist secret
                    },
                    {"source": "sidecar"},
                ),
            ),
        ):
            mock_settings.MINIMAX_API_KEY = ""
            mock_settings.MINIMAX_HOST = ""
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = self.PAYLOAD
            mock_http_client.get.return_value = response

            await collector.collect(mock_http_client)

        assert (
            mock_http_client.get.call_args[0][0] == "https://api.minimax.io/v1/coding_plan/remains"
        )


class TestMiniMaxCredentialSelection:
    """Which credential kind is used, and what a rejected token reports.

    MiniMax answers HTTP 200 and puts the failure in the body: sending the
    wrong kind of credential to a host comes back as
    ``base_resp.status_code`` 1004 "login fail". That has to read as a
    failure — parsed as a quota snapshot it would report a healthy
    "No active plan" card, and because this collector reports complete
    snapshots the reconciler would then retire the user's real rows.
    """

    LOGIN_FAIL = {"base_resp": {"status_code": 1004, "status_msg": "login fail"}}

    @pytest.mark.asyncio
    async def test_login_fail_is_an_error_card_not_an_empty_plan(self, mock_http_client):
        """A rejected token must not look like a plan with no quota."""
        with (
            patch("app.services.collectors.minimax.settings") as mock_settings,
            patch(
                "app.services.collectors.minimax.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.token_cache.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=(
                    {"oauth_token": "mmoat_test_token"},  # pragma: allowlist secret
                    {"source": "sidecar"},
                ),
            ),
        ):
            mock_settings.MINIMAX_API_KEY = ""
            mock_settings.MINIMAX_HOST = ""
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = self.LOGIN_FAIL
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        card = result[0]
        assert card["remaining"] == "ERR"
        assert card["error_type"] == "auth_failed"
        assert "1004" in card["detail"]
        # An error card keeps the smart collector from treating the response
        # as a complete snapshot, which is what would delete the real rows.
        assert is_error_card_dict(card)
        assert not any(c.get("remaining") == "No active plan" for c in result)

    @pytest.mark.asyncio
    async def test_other_failure_status_is_an_api_error(self, mock_http_client):
        """A non-zero status that isn't a login failure is still a failure."""
        with (
            patch("app.services.collectors.minimax.settings") as mock_settings,
            patch(
                "app.services.collectors.minimax.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.token_cache.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=(
                    {"oauth_token": "mmoat_test_token"},  # pragma: allowlist secret
                    {"source": "sidecar"},
                ),
            ),
        ):
            mock_settings.MINIMAX_API_KEY = ""
            mock_settings.MINIMAX_HOST = ""
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = {
                "base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}
            }
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert result[0]["error_type"] == "api_error"
        assert "1008" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_pasted_app_token_is_recognised_by_its_prefix(self, mock_http_client):
        """A token pasted into Settings arrives without a marker of its kind.

        The ``mmoat_`` prefix is all that says it is the app's OAuth token and
        not a platform API secret — without it the token would be sent to the
        API host, which answers "login fail".
        """
        with (
            patch("app.services.collectors.minimax.settings") as mock_settings,
            patch(
                "app.services.collectors.minimax.credential_provider.get_provider_api_key",
                return_value="mmoat_pasted_token",  # pragma: allowlist secret
            ),
            patch(
                "app.services.token_cache.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_settings.MINIMAX_API_KEY = ""
            mock_settings.MINIMAX_HOST = ""
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = TestMiniMaxOAuthCollector.PAYLOAD
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert (
            mock_http_client.get.call_args[0][0]
            == "https://platform.minimax.io/v1/api/openplatform/coding_plan/remains"
        )
        assert result[0]["detail"].endswith("[App]")

    @pytest.mark.asyncio
    async def test_pasted_api_secret_keeps_using_the_api_host(self, mock_http_client):
        """The platform secret has no such prefix and stays on the API host."""
        with (
            patch("app.services.collectors.minimax.settings") as mock_settings,
            patch(
                "app.services.collectors.minimax.credential_provider.get_provider_api_key",
                return_value="sk_pasted_key",  # pragma: allowlist secret
            ),
            patch(
                "app.services.token_cache.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_settings.MINIMAX_API_KEY = ""
            mock_settings.MINIMAX_HOST = ""
            collector = MiniMaxCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 200
            response.json.return_value = TestMiniMaxOAuthCollector.PAYLOAD
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert (
            mock_http_client.get.call_args[0][0] == "https://api.minimax.io/v1/coding_plan/remains"
        )
        assert result[0]["detail"].endswith("[API]")

    def test_china_host_is_matched_on_the_parsed_hostname(self):
        """The China host is decided by the parsed hostname, not a raw suffix.

        A suffix test would also accept a lookalike domain such as
        ``evilminimaxi.com``.
        """
        collector = MiniMaxCollector()
        collector.host = "minimaxi.com"
        assert collector._open_platform_base_url() == "https://www.minimaxi.com"
        collector.host = "www.minimaxi.com"
        assert collector._open_platform_base_url() == "https://www.minimaxi.com"
        collector.host = "api.minimaxi.com"
        assert collector._open_platform_base_url() == "https://www.minimaxi.com"
        collector.host = "evilminimaxi.com"
        assert collector._open_platform_base_url() == "https://platform.evilminimaxi.com"
        collector.host = "minimax.io"
        assert collector._open_platform_base_url() == "https://platform.minimax.io"
        # A host configured as a URL is still a host — the raw setting must
        # not be pasted into the middle of the path.
        collector.host = "https://minimax.io"
        assert collector._open_platform_base_url() == "https://platform.minimax.io"

    def test_the_host_setting_is_normalised_before_a_host_is_built(self):
        """A host that carries a scheme must not be pasted into a URL.

        The setting is documented as a bare domain, but a value that arrives
        with a scheme is normalised to its host, so neither the API host nor
        the open-platform host ends up with ``https://`` in the middle of it.
        """
        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_HOST = "https://minimax.io"
            collector = MiniMaxCollector()
            assert collector._get_host() == "minimax.io"
            collector.host = collector._get_host()
            assert collector._api_base_url() == "https://api.minimax.io"
            assert collector._open_platform_base_url() == "https://platform.minimax.io"

            mock_settings.MINIMAX_HOST = "minimaxi.com"
            collector = MiniMaxCollector()
            assert collector._get_host() == "minimaxi.com"
            collector.host = collector._get_host()
            assert collector._api_base_url() == "https://api.minimaxi.com"
            assert collector._open_platform_base_url() == "https://www.minimaxi.com"

            # The default is unchanged, and a port is kept.
            mock_settings.MINIMAX_HOST = ""
            collector = MiniMaxCollector()
            assert collector._get_host() == "minimax.io"
            mock_settings.MINIMAX_HOST = "https://minimax.io:8443"
            collector = MiniMaxCollector()
            assert collector._get_host() == "minimax.io:8443"
