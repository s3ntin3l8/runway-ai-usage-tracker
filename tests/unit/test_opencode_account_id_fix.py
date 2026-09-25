"""Tests for the OpenCode collector's auth & card-building logic.

Two surfaces are exercised:

- ``_get_opencode_api`` / ``_build_cards_from_go_status``: Bearer-token path
  against ``/console/api/go_status``. Asserts the three Go-tier windows are
  emitted with correct ``pct_used``, ``limit_value``, ``reset_at`` and
  ``window_type`` (the dashboard's universal contract). Also asserts the
  rate-limited status short-circuits to ``health == "critical"``.

- ``_get_opencode_web``: Console session-cookie 2-step handshake. Asserts
  workspace discovery + ``x-org-id`` header + auth-failure surfaces a card
  instead of the pre-fix silent ``[]``.

- ``_error_handler``: when every strategy fails (no api_key, no cookies),
  the collector emits an auth_failed card with the documented message —
  not a silent blank dashboard.

These tests run without network access: ``http_request_with_retry`` is
patched to return pre-canned ``httpx.Response`` objects carrying the JSON
payloads that the real opencode console API returns (captured during the
investigation).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.collectors.opencode import (
    _DEFAULT_LIMIT_USD,
    _WINDOW_TYPE_MAP,
    OpenCodeCollector,
)


def _json_response(body: dict, status_code: int = 200) -> httpx.Response:
    """httpx.Response carrying a JSON body."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=body)
    resp.text = json.dumps(body)
    return resp


def _go_status_body() -> dict:
    """Real ``/console/api/go/status`` body captured during investigation."""
    return {
        "subscriberUserId": "acc_01KNHVQCQM7SGA77M43TZ72J9W",
        "product": "go",
        "access": {
            "startsAt": "2026-09-06T18:44:43.000Z",
            "endsAt": "2026-10-06T18:44:43.000Z",
            "meters": {
                "fiveHour": {
                    "startsAt": None,
                    "resetsAt": None,
                    "limitMicroCents": "1200000000",
                    "usedMicroCents": "0",
                },
                "week": {
                    "startsAt": "2026-09-21T00:00:00.000Z",
                    "resetsAt": "2026-09-28T00:00:00.000Z",
                    "limitMicroCents": "3000000000",
                    "usedMicroCents": "550943739",
                },
                "month": {
                    "limitMicroCents": "6000000000",
                    "usedMicroCents": "6000000000",
                },
            },
        },
    }


def _zen_usage_body() -> dict:
    """Real ``/zen/go/v1/usage`` body captured during investigation."""
    return {
        "usage": {
            "rolling": {
                "status": "ok",
                "percent": 0,
                "resetsAt": "2026-09-25T03:07:47.695Z",
            },
            "weekly": {
                "status": "ok",
                "percent": 18,
                "resetsAt": "2026-09-28T00:00:00.000Z",
            },
            "monthly": {
                "status": "rate-limited",
                "percent": 100,
                "resetsAt": "2026-10-06T18:44:43.000Z",
            },
        }
    }


class TestBuildCardsFromGoStatus:
    """Pure-function test: builds cards from the console endpoint body."""

    def test_emits_three_cards_with_canonical_window_types(self):
        collector = OpenCodeCollector(account_id="acc_test")
        cards = collector._build_cards_from_go_status(_go_status_body(), input_source="config")
        assert len(cards) == 3
        window_types = sorted(c["window_type"] for c in cards)
        assert window_types == ["monthly", "session", "weekly"]

    def test_card_uses_microcent_math(self):
        """1 micro-cent = $0.00000001. Verify the conversion and pct."""
        collector = OpenCodeCollector(account_id="acc_test")
        cards = collector._build_cards_from_go_status(_go_status_body(), input_source="config")
        by_window = {c["window_type"]: c for c in cards}

        # week: used 550943739 / 1e8 ≈ $5.51; limit $30 → pct ≈ 18.36
        week = by_window["weekly"]
        assert week["limit_value"] == pytest.approx(30.0, rel=1e-6)
        assert week["used_value"] == pytest.approx(5.50943739, rel=1e-6)
        assert week["pct_used"] == pytest.approx(18.36, rel=1e-2)
        assert week["currency"] == "USD"
        assert week["tier"] == "Go"
        assert week["reset_at"].startswith("2026-09-28T")

        # month: used == limit → pct = 100
        month = by_window["monthly"]
        assert month["pct_used"] == pytest.approx(100.0, rel=1e-6)
        assert month["health"] == "critical"

    def test_missing_limit_falls_back_to_default(self):
        """When limitMicroCents is absent, use the documented default."""
        body = {
            "access": {
                "meters": {
                    "fiveHour": {"usedMicroCents": "0"},
                    "week": {"usedMicroCents": "0"},
                    "month": {"usedMicroCents": "0"},
                }
            }
        }
        collector = OpenCodeCollector(account_id="acc_test")
        cards = collector._build_cards_from_go_status(body, input_source="config")
        assert len(cards) == 3
        for card in cards:
            window = next(k for k, v in _WINDOW_TYPE_MAP.items() if v == card["window_type"])
            assert card["limit_value"] == _DEFAULT_LIMIT_USD[window]


class TestBuildCardsFromZenUsage:
    def test_emits_three_cards_from_percentages(self):
        collector = OpenCodeCollector(account_id="acc_test")
        cards = collector._build_cards_from_zen_usage(_zen_usage_body(), input_source="config")
        assert len(cards) == 3
        by_window = {c["window_type"]: c for c in cards}

        # rolling: 0% of $12
        assert by_window["session"]["pct_used"] == 0.0
        assert by_window["session"]["limit_value"] == 12.0

        # weekly: 18% of $30 → used ≈ 5.40
        assert by_window["weekly"]["pct_used"] == 18.0
        assert by_window["weekly"]["used_value"] == pytest.approx(5.4, rel=1e-3)

        # monthly: rate-limited even though pct == 100 → critical anyway.
        assert by_window["monthly"]["pct_used"] == 100.0
        assert by_window["monthly"]["health"] == "critical"


class TestGetOpencodeApi:
    """Bearer-token strategy against mocked /console/api/go/status."""

    @pytest.mark.asyncio
    async def test_successful_fetch_returns_three_cards(self):
        collector = OpenCodeCollector(account_id="acc_test")

        # Patch token cache so the collector finds the API key.
        async def fake_get_with_metadata(provider, account_id=None):
            assert provider == "opencode"
            return ({"api_key": "oc_sk_test"}, {"source": "sidecar"})

        with (
            patch(
                "app.services.collectors.opencode.token_cache.get_with_metadata",
                side_effect=fake_get_with_metadata,
            ),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=_json_response(_go_status_body()),
            ) as mock_http,
        ):
            cards = await collector._get_opencode_api(MagicMock())

        assert len(cards) == 3
        assert all(c["tier"] == "Go" for c in cards)
        assert mock_http.await_args.kwargs["headers"]["Authorization"] == "Bearer oc_sk_test"

    @pytest.mark.asyncio
    async def test_no_api_key_returns_error_card(self):
        """The pre-fix collector returned ``[]``; the new code returns an
        auth_failed error card so the dashboard is no longer silently blank."""
        collector = OpenCodeCollector(account_id="acc_test")

        async def fake_get_with_metadata(provider, account_id=None):
            return ({"cookie_session": "x"}, {"source": "config"})

        with patch(
            "app.services.collectors.opencode.token_cache.get_with_metadata",
            side_effect=fake_get_with_metadata,
        ):
            cards = await collector._get_opencode_api(MagicMock())

        assert cards == []
        # Now drive _error_handler via _primary_strategy fallback path
        # (BaseCollector.collect calls _error_handler() when _primary returns [])
        err = await collector._error_handler()
        assert err and err[0]["error_type"] == "auth_failed"
        assert (
            "oc_sk" in err[0]["detail"]
            or "API key" in err[0]["detail"]
            or "session expired" in err[0]["detail"].lower()
        )

    @pytest.mark.asyncio
    async def test_401_sets_invalid_api_key_and_emits_error(self):
        collector = OpenCodeCollector(account_id="acc_test")

        async def fake_get_with_metadata(provider, account_id=None):
            return ({"api_key": "oc_sk_bad"}, {"source": "config"})

        bad_resp = MagicMock(spec=httpx.Response)
        bad_resp.status_code = 401
        bad_resp.text = '{"_tag":"Unauthorized"}'

        with (
            patch(
                "app.services.collectors.opencode.token_cache.get_with_metadata",
                side_effect=fake_get_with_metadata,
            ),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=bad_resp,
            ),
        ):
            cards = await collector._get_opencode_api(MagicMock())

        assert cards == []
        assert collector._last_error_reason == "invalid_api_key"
        err = await collector._error_handler()
        assert err[0]["error_type"] == "auth_failed"


class TestGetOpencodeWeb:
    """Console-cookie 2-step handshake against mocked endpoints."""

    @pytest.mark.asyncio
    async def test_successful_handshake_returns_cards(self):
        collector = OpenCodeCollector(account_id="acc_test")
        orgs_body = [{"id": "wrk_test_workspace", "name": "Default"}]

        async def fake_get_with_metadata(provider, account_id=None):
            return (
                {
                    "cookie_session": "fake_auth",
                    "console_session": "st_fake",
                },
                {"source": "config"},
            )

        with (
            patch(
                "app.services.collectors.opencode.token_cache.get_with_metadata",
                side_effect=fake_get_with_metadata,
            ),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[
                    _json_response(orgs_body),  # /console/api/orgs
                    _json_response(_go_status_body()),  # /console/api/go/status
                ],
            ) as mock_http,
        ):
            cards = await collector._get_opencode_web(MagicMock())

        assert len(cards) == 3
        # Confirm the x-org-id header was set on the second call.
        second_call_headers = mock_http.call_args_list[1].kwargs["headers"]
        assert second_call_headers.get("x-org-id") == "wrk_test_workspace"
        assert "auth=fake_auth" in second_call_headers["Cookie"]
        assert "__Host-console_session=st_fake" in second_call_headers["Cookie"]

    @pytest.mark.asyncio
    async def test_401_on_orgs_returns_error_card(self):
        """Cookie session expired: surface an auth_failed card, not silence."""
        collector = OpenCodeCollector(account_id="acc_test")
        bad_resp = MagicMock(spec=httpx.Response)
        bad_resp.status_code = 401
        bad_resp.text = '{"_tag":"Unauthorized"}'

        async def fake_get_with_metadata(provider, account_id=None):
            return ({"cookie_session": "expired"}, {"source": "config"})

        with (
            patch(
                "app.services.collectors.opencode.token_cache.get_with_metadata",
                side_effect=fake_get_with_metadata,
            ),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=bad_resp,
            ),
        ):
            cards = await collector._get_opencode_web(MagicMock())

        assert cards == []
        # The 401 from /console/api/orgs must stay diagnosed as
        # session_invalid (auth_failed) — the generic no_workspace must
        # not mask it (PR #339 round-1 review).
        assert collector._last_error_reason == "session_invalid"
        err = await collector._error_handler()
        assert err[0]["error_type"] == "auth_failed"

    @pytest.mark.asyncio
    async def test_empty_orgs_still_reports_no_workspace(self):
        """The non-401 workspace failure (200 but empty org list) keeps
        the generic no_workspace diagnosis — the session_invalid guard
        only suppresses it when a specific reason was already set."""
        collector = OpenCodeCollector(account_id="acc_test")
        empty_resp = MagicMock(spec=httpx.Response)
        empty_resp.status_code = 200
        empty_resp.json.return_value = []

        async def fake_get_with_metadata(provider, account_id=None):
            return ({"cookie_session": "valid"}, {"source": "config"})

        with (
            patch(
                "app.services.collectors.opencode.token_cache.get_with_metadata",
                side_effect=fake_get_with_metadata,
            ),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=empty_resp,
            ),
        ):
            cards = await collector._get_opencode_web(MagicMock())

        assert cards == []
        assert collector._last_error_reason == "no_workspace"
        err = await collector._error_handler()
        assert err[0]["error_type"] == "parse_error"


class TestIsConfigured:
    @pytest.mark.asyncio
    async def test_true_when_api_key_present(self):
        collector = OpenCodeCollector(account_id="acc_test")

        async def fake(provider, token_type, account_id=None):
            return "oc_sk_xyz" if token_type == "api_key" else None

        with patch(
            "app.services.collectors.opencode.token_cache.get_token",
            side_effect=fake,
        ):
            assert await collector.is_configured() is True

    @pytest.mark.asyncio
    async def test_true_when_cookie_session_present(self):
        collector = OpenCodeCollector(account_id="acc_test")

        async def fake(provider, token_type, account_id=None):
            return "auth_value" if token_type == "cookie_session" else None

        with patch(
            "app.services.collectors.opencode.token_cache.get_token",
            side_effect=fake,
        ):
            assert await collector.is_configured() is True

    @pytest.mark.asyncio
    async def test_false_when_no_credentials(self):
        collector = OpenCodeCollector(account_id="acc_test")
        with patch(
            "app.services.collectors.opencode.token_cache.get_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            assert await collector.is_configured() is False


class TestMicrocentsToUsd:
    def test_converts_correctly(self):
        assert OpenCodeCollector._microcents_to_usd("100000000") == 1.0
        assert OpenCodeCollector._microcents_to_usd("550943739") == pytest.approx(5.50943739)
        assert OpenCodeCollector._microcents_to_usd(0) == 0.0
        assert OpenCodeCollector._microcents_to_usd(None) is None
