"""Tests for the xAI (Grok) cli-chat-proxy bearer collector."""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.collectors.xai import XaiCollector


def _make_jwt(exp_unix_seconds: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "ES256"}).encode()).rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp_unix_seconds, "sub": "x"}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


def _make_response(body: dict, status: int = 200) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json = MagicMock(return_value=body)
    resp.text = json.dumps(body)
    return resp


WEEKLY_BILLING = {
    "config": {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-09-24T23:01:54Z",
            "end": "2026-10-01T23:01:54Z",
        },
        "creditUsagePercent": 18.0,
        "onDemandCap": {"val": 0},
        "onDemandUsed": {"val": 0},
        "productUsage": [{"product": "GrokBuild", "usagePercent": 18.0}],
    }
}

SETTINGS_SUPERGROK_HEAVY = {"subscription_tier_display": "SuperGrok Heavy"}
SETTINGS_SUPERGROK = {"subscription_tier_display": "SuperGrok"}


class TestExtractExpMs:
    def test_jwt_with_seconds_payload(self):
        exp_s = int(time.time()) + 3600
        jwt = _make_jwt(exp_s)
        assert XaiCollector._extract_exp_ms(jwt, None) == exp_s * 1000

    def test_jwt_with_ms_payload(self):
        exp_ms = int((time.time() + 3600) * 1000)
        jwt = _make_jwt(exp_ms)
        assert XaiCollector._extract_exp_ms(jwt, None) == exp_ms

    def test_no_jwt_no_exp_returns_none(self):
        assert XaiCollector._extract_exp_ms("not-a-jwt", None) is None


class TestIsExpired:
    def test_expired_token(self):
        c = XaiCollector(account_id="acc_test")
        assert c._is_expired(_make_jwt(int(time.time()) - 7200)) is True

    def test_fresh_token(self):
        c = XaiCollector(account_id="acc_test")
        assert c._is_expired(_make_jwt(int(time.time()) + 86400)) is False

    def test_unparseable_token_returns_not_expired(self):
        """Malformed tokens shouldn't false-positive as expired; the real
        401 from the API will surface the failure."""
        c = XaiCollector(account_id="acc_test")
        assert c._is_expired("garbage") is False


class TestBuildCardsFromBilling:
    def test_emits_weekly_credits_card(self):
        c = XaiCollector(account_id="acc_test")
        cards = c._build_cards_from_billing(WEEKLY_BILLING)
        assert len(cards) == 1
        card = cards[0]
        assert card["provider_id"] == "xai"
        assert card["window_type"] == "weekly"
        assert card["pct_used"] == 18.0
        assert card["unit_type"] == "percent"
        assert card["reset_at"].startswith("2026-10-01T")
        assert card["tier"] is None  # tier is set after settings call

    def test_emits_on_demand_card_when_cap_nonzero(self):
        body = {
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-09-24T23:01:54Z",
                    "end": "2026-10-01T23:01:54Z",
                },
                "creditUsagePercent": 5.0,
                "onDemandCap": {"val": 50000000000},  # $500
                "onDemandUsed": {"val": 12345678},  # $0.12
            }
        }
        c = XaiCollector(account_id="acc_test")
        cards = c._build_cards_from_billing(body)
        assert len(cards) == 2
        on_demand = next(c1 for c1 in cards if c1["unit_type"] == "currency")
        assert on_demand["currency"] == "USD"
        assert on_demand["limit_value"] == pytest.approx(500.0, rel=1e-6)
        assert on_demand["used_value"] == pytest.approx(0.12345678, rel=1e-6)
        assert on_demand["pct_used"] < 1.0  # microcents math

    def test_monthly_period_maps_to_monthly_window(self):
        body = {
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_MONTHLY",
                    "start": "2026-09-01T00:00:00Z",
                    "end": "2026-10-01T00:00:00Z",
                },
                "creditUsagePercent": 42.0,
            }
        }
        c = XaiCollector(account_id="acc_test")
        cards = c._build_cards_from_billing(body)
        assert len(cards) == 1
        assert cards[0]["window_type"] == "monthly"

    def test_unknown_period_type_falls_back_to_monthly(self):
        body = {"config": {"currentPeriod": {"type": "WEIRD"}, "creditUsagePercent": 1.0}}
        c = XaiCollector(account_id="acc_test")
        cards = c._build_cards_from_billing(body)
        assert cards[0]["window_type"] == "monthly"

    def test_missing_credit_percent_skips_credits_card(self):
        body = {"config": {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"}}}
        c = XaiCollector(account_id="acc_test")
        cards = c._build_cards_from_billing(body)
        assert cards == []
        assert c._last_error_reason == "parse_error"

    def test_empty_config_returns_empty(self):
        c = XaiCollector(account_id="acc_test")
        assert c._build_cards_from_billing({}) == []

    def test_annual_period_falls_back_to_monthly_window(self):
        """xAI doesn't publish a YEARLY window_type yet — unknown period
        types fall back to ``monthly`` per the collector's documented
        mapping (see xai.py ``_PERIOD_WINDOW_TYPE`` + comment)."""
        body = {
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_ANNUAL",
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2027-01-01T00:00:00Z",
                },
                "creditUsagePercent": 7.5,
            }
        }
        c = XaiCollector(account_id="acc_test")
        cards = c._build_cards_from_billing(body)
        assert len(cards) == 1
        assert cards[0]["window_type"] == "monthly"
        assert cards[0]["pct_used"] == 7.5


class TestGetXaiApi:
    """End-to-end tests of ``_get_xai_api`` with mocked HTTP responses.

    Note: the order of HTTP calls matters — ``_fetch_plan_tier`` runs first
    (settings), then the billing call. Mock side-effects are listed in the
    order they're consumed.
    """

    @pytest.mark.asyncio
    async def test_emits_credits_card_with_plan_tier(self):
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return "eyJ.eyJ.zzz" if token_type == "xai_access" else None

        with (
            patch(
                "app.services.collectors.xai.token_cache.get_token",
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.xai.http_request_with_retry",
                new_callable=AsyncMock,
                # _fetch_plan_tier runs first → settings; then billing.
                side_effect=[
                    _make_response(SETTINGS_SUPERGROK_HEAVY),
                    _make_response(WEEKLY_BILLING),
                ],
            ),
        ):
            cards = await c._get_xai_api(MagicMock())

        assert len(cards) == 1
        assert cards[0]["tier"] == "SuperGrok Heavy"
        assert cards[0]["pct_used"] == 18.0
        assert c._plan_tier == "SuperGrok Heavy"

    @pytest.mark.asyncio
    async def test_settings_failure_does_not_block_quota(self):
        """Best-effort enrichment: settings 500 must not stop the quota card."""
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return "eyJ.eyJ.zzz" if token_type == "xai_access" else None

        with (
            patch(
                "app.services.collectors.xai.token_cache.get_token",
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.xai.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[
                    _make_response({}, status=500),  # settings fails
                    _make_response(WEEKLY_BILLING),  # billing OK
                ],
            ),
        ):
            cards = await c._get_xai_api(MagicMock())

        assert len(cards) == 1
        assert cards[0]["tier"] is None  # enrichment failed, no badge
        assert cards[0]["pct_used"] == 18.0  # quota card still built

    @pytest.mark.asyncio
    async def test_expired_token_short_circuits(self):
        """Expired JWT → empty list → base collector's _error_handler emits
        the auth_required card; the real API call never goes out."""
        c = XaiCollector(account_id="acc_test")
        expired = _make_jwt(int(time.time()) - 7200)

        async def fake_get_token(provider, token_type, account_id=None):
            return expired if token_type == "xai_access" else None

        with (
            patch(
                "app.services.collectors.xai.token_cache.get_token",
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.xai.http_request_with_retry",
                new_callable=AsyncMock,
            ) as mock_http,
        ):
            cards = await c.collect(MagicMock())

        assert cards[0]["error_type"] == "auth_failed"
        assert c._last_error_reason == "invalid_api_key"
        assert mock_http.call_count == 0

    @pytest.mark.asyncio
    async def test_billing_401_sets_invalid_api_key(self):
        """401 from /v1/billing means the token is bad even if not yet
        expired — surface an auth_required card."""
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return _make_jwt(int(time.time()) + 86400) if token_type == "xai_access" else None

        with (
            patch(
                "app.services.collectors.xai.token_cache.get_token",
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.xai.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[
                    # settings OK (first call: _fetch_plan_tier)
                    _make_response(SETTINGS_SUPERGROK),
                    # billing 401 (second call: actual quota fetch)
                    _make_response({"error": "unauthenticated"}, status=401),
                ],
            ),
        ):
            cards = await c._get_xai_api(MagicMock())

        assert cards == []
        assert c._last_error_reason == "invalid_api_key"

    @pytest.mark.asyncio
    async def test_billing_401_through_collect_emits_auth_error_card(self):
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return _make_jwt(int(time.time()) + 86400) if token_type == "xai_access" else None

        with (
            patch(
                "app.services.collectors.xai.token_cache.get_token",
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.xai.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[
                    _make_response(SETTINGS_SUPERGROK),
                    _make_response({"error": "unauthenticated"}, status=401),
                ],
            ),
        ):
            cards = await c.collect(MagicMock())

        assert cards[0]["error_type"] == "auth_failed"
        assert "re-login" in cards[0]["detail"]

    @pytest.mark.asyncio
    async def test_no_access_token_returns_empty(self):
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return None

        with patch(
            "app.services.collectors.xai.token_cache.get_token",
            side_effect=fake_get_token,
        ):
            cards = await c._get_xai_api(MagicMock())

        assert cards == []

    @pytest.mark.asyncio
    async def test_billing_500_returns_empty(self):
        """5xx from /v1/billing is a server-side fault, not auth — returns
        [] with ``_last_error_reason`` left at default. BaseCollector's
        default heuristic (empty == error) routes this to ``_error_handler``
        which emits the generic 'quota collection failed' card."""
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return _make_jwt(int(time.time()) + 86400) if token_type == "xai_access" else None

        with (
            patch(
                "app.services.collectors.xai.token_cache.get_token",
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.xai.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[
                    _make_response(SETTINGS_SUPERGROK),
                    _make_response({"error": "boom"}, status=500),
                ],
            ),
        ):
            cards = await c._get_xai_api(MagicMock())

        assert cards == []
        assert c._last_error_reason == "unknown"

    @pytest.mark.asyncio
    async def test_billing_timeout_returns_empty(self):
        """Network timeout on /v1/billing — the retry helper raises
        TimeoutException, the strategy catches and returns ``[]``. Base
        default treats empty as error → ``_error_handler`` runs."""
        import httpx

        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return _make_jwt(int(time.time()) + 86400) if token_type == "xai_access" else None

        async def fake_http(*args, **kwargs):
            raise httpx.TimeoutException("boom")

        with (
            patch(
                "app.services.collectors.xai.token_cache.get_token",
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.xai.http_request_with_retry",
                side_effect=fake_http,
            ),
        ):
            cards = await c._get_xai_api(MagicMock())

        assert cards == []

    @pytest.mark.asyncio
    async def test_billing_malformed_json_returns_empty(self):
        """Non-JSON 200 from /v1/billing — parse_error path. _last_error_reason
        set to ``parse_error`` so ``_error_handler`` emits the generic
        'quota collection failed' card."""
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return _make_jwt(int(time.time()) + 86400) if token_type == "xai_access" else None

        bad_resp = MagicMock(spec=httpx.Response)
        bad_resp.status_code = 200
        bad_resp.json = MagicMock(side_effect=ValueError("not json"))
        bad_resp.text = "<html>oops</html>"

        with (
            patch(
                "app.services.collectors.xai.token_cache.get_token",
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.xai.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[
                    _make_response(SETTINGS_SUPERGROK),
                    bad_resp,
                ],
            ),
        ):
            cards = await c._get_xai_api(MagicMock())

        assert cards == []
        assert c._last_error_reason == "parse_error"

    @pytest.mark.asyncio
    async def test_malformed_jwt_does_not_short_circuit(self):
        """Garbage in xai_access: ``_is_expired`` returns False (we don't
        false-positive on undecodable tokens — let the real API call's
        401 surface it). The call goes out and the API's 401 path takes
        over."""
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return "not.a.real.jwt" if token_type == "xai_access" else None

        with (
            patch(
                "app.services.collectors.xai.token_cache.get_token",
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.xai.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[
                    _make_response(SETTINGS_SUPERGROK),
                    _make_response({"error": "unauthenticated"}, status=401),
                ],
            ),
        ):
            cards = await c._get_xai_api(MagicMock())

        assert cards == []
        assert c._last_error_reason == "invalid_api_key"

    @pytest.mark.asyncio
    async def test_billing_403_returns_empty_with_invalid_api_key(self):
        """403 from /v1/billing — token valid but lacks scope; surface as
        ``auth_required`` (matches the 401 path)."""
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return _make_jwt(int(time.time()) + 86400) if token_type == "xai_access" else None

        with (
            patch(
                "app.services.collectors.xai.token_cache.get_token",
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.xai.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[
                    _make_response(SETTINGS_SUPERGROK),
                    _make_response({"error": "forbidden"}, status=403),
                ],
            ),
        ):
            cards = await c._get_xai_api(MagicMock())

        assert cards == []
        assert c._last_error_reason == "invalid_api_key"

    @pytest.mark.asyncio
    async def test_collect_empty_results_falls_through_to_error_handler(self):
        """Hermes Finding 2 fix: BaseCollector.collect() must call
        ``_error_handler`` when ``_primary_strategy`` returns ``[]`` (the
        default ``_is_error_result`` heuristic). Without this, an expired
        token would silently produce an empty card list and the user would
        see nothing actionable on the dashboard."""

        c = XaiCollector(account_id="acc_test")
        expired = _make_jwt(int(time.time()) - 7200)

        async def fake_get_token(provider, token_type, account_id=None):
            return expired if token_type == "xai_access" else None

        with patch(
            "app.services.collectors.xai.token_cache.get_token",
            side_effect=fake_get_token,
        ):
            # ``c.collect`` is the public entry; runs through
            # ``_resolve_strategies`` -> ``_primary_strategy`` -> ``_get_xai_api``
            # which short-circuits on the expired JWT, then
            # ``_is_error_result`` -> True -> ``_error_handler``.
            cards = await c.collect(MagicMock())

        assert len(cards) == 1
        card = cards[0]
        # error_card helper puts the message under ``detail`` (truncated
        # to 40 chars) and marks ``remaining="ERR"``. Just check the
        # auth_failed signal made it through.
        assert card.get("remaining") == "ERR"
        assert card.get("error_type") == "auth_failed"
        assert (
            "opencode" in card.get("detail", "").lower()
            or "expired" in card.get("detail", "").lower()
        )


class TestIsConfigured:
    @pytest.mark.asyncio
    async def test_true_when_xai_access_present(self):
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return "x" if token_type == "xai_access" else None

        with patch(
            "app.services.collectors.xai.token_cache.get_token",
            side_effect=fake_get_token,
        ):
            assert await c.is_configured() is True

    @pytest.mark.asyncio
    async def test_true_when_xai_refresh_present(self):
        """Refresh-only (no access) still counts as configured — the
        collector's is_configured doesn't distinguish; _is_expired will
        catch the bad access and surface the auth_required card."""
        c = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return "x" if token_type == "xai_refresh" else None

        with patch(
            "app.services.collectors.xai.token_cache.get_token",
            side_effect=fake_get_token,
        ):
            assert await c.is_configured() is True

    @pytest.mark.asyncio
    async def test_false_when_no_xai_token(self):
        c = XaiCollector(account_id="acc_test")
        with patch(
            "app.services.collectors.xai.token_cache.get_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            assert await c.is_configured() is False
