"""Unit tests for Kimi quota collectors (Kimi API, Kimi K2, Kimi Coding)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.collectors.kimi_api import KimiApiCollector
from app.services.collectors.kimi_coding import KimiCodingCollector
from app.services.collectors.kimi_k2 import KimiK2Collector


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
    """Test suite for Kimi Coding (IDE) collector (api + web strategies)."""

    # Live-verified Pro plan response from GET api.kimi.com/coding/v1/usages.
    CODE_API_PRO_RESPONSE = {
        "limits": [
            {
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {
                    "limit": "100",
                    "used": "41",
                    "remaining": "59",
                    "resetTime": "2026-09-18T18:52:05Z",
                },
            }
        ],
        "usages": {
            "limit_5h": {"used_ratio": 0, "reset_time": "2026-09-18T18:52:04Z"},
            "limit_month_total": {"used_ratio": 0.011, "reset_time": "2026-10-19T00:00:00Z"},
            "limit_month_code": {"used_ratio": 0.0, "reset_time": "2026-10-19T00:00:00Z"},
        },
        "booster_wallet": {"status": "STATUS_DISABLED"},
    }

    # Live-verified web gateway responses (kimi-auth cookie auth).
    WEB_USAGES_RESPONSE = {
        "usages": [
            {
                "scope": "FEATURE_CODING",
                "detail": {"limit": "100", "remaining": "100", "resetTime": "2026-09-25T13:52:05Z"},
                "limits": [
                    {
                        "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {
                            "limit": "100",
                            "used": "47",
                            "remaining": "53",
                            "resetTime": "2026-09-18T18:52:05Z",
                        },
                    }
                ],
            }
        ],
        "totalQuota": {"limit": "100", "used": "1", "remaining": "99"},
    }
    WEB_STATS_RESPONSE = {
        "ratelimitCode5h": {"ratio": 0.4703, "enabled": True, "resetTime": "2026-09-18T18:52:05Z"},
        "ratelimitCode7d": {"ratio": 0.0038, "enabled": True, "resetTime": "2026-09-25T13:52:05Z"},
        "subscriptionBalance": {
            "amountUsedRatio": 0.0128,
            "kimiCodeUsedRatio": 0.0128,
            "expireTime": "2026-10-19T00:00:00Z",
        },
    }
    WEB_SUBSCRIPTION_RESPONSE = {
        "subscription": {
            "active": True,
            "status": "SUBSCRIPTION_STATUS_ACTIVE",
            "goods": {"title": "Pro"},
        }
    }

    # Live-verified GET /coding/v1/me response (API key auth).
    CODE_API_ME_RESPONSE = {
        "user_id": "d78kbuol3dc8u30k4q6g",
        "global_id": "d78kbuol3dc8u30k4q6g",
        "nickname": "Test User",
        "status": "USER_STATUS_NORMAL",
        "region": "REGION_OVERSEA",
        "user_level": 25,
        "user_level_name": "Pro",
        "goods_version": 2,
        "email": "user@kimi.test",
        "created_time": "2026-04-04T17:10:19.275499Z",
        "last_login_time": "2026-09-18T13:48:11.736784Z",
    }

    @staticmethod
    def _patch_credentials(api_key=None, session_cookie=None, creds=None):
        """Patch the credential_provider seen by the kimi_coding module."""
        patcher = patch("app.services.collectors.kimi_coding.credential_provider")
        mock_cp = patcher.start()
        mock_cp.get_provider_api_key.return_value = api_key
        mock_cp.get_provider_session_cookie.return_value = session_cookie
        mock_cp.get_credentials.return_value = creds if creds is not None else {}
        return patcher

    @staticmethod
    def _mock_settings(**overrides):
        """settings patch with sane kimi defaults (patched MagicMock attrs are truthy)."""
        patcher = patch("app.services.collectors.kimi_coding.settings")
        mock_settings = patcher.start()
        mock_settings.KIMI_AUTH_TOKEN = ""
        mock_settings.KIMI_CODE_API_KEY = ""
        mock_settings.KIMI_CODE_BASE_URL = ""
        for key, value in overrides.items():
            setattr(mock_settings, key, value)
        return patcher

    def _http_router(self, mock_http_client, routes):
        """Route mocked HTTP calls by URL substring; unmatched calls get a 200 {}."""

        async def _request(method, url, **kwargs):
            for needle, payload in routes:
                if needle in str(url):
                    resp = MagicMock(spec=httpx.Response)
                    if isinstance(payload, int):
                        resp.status_code = payload
                    else:
                        resp.status_code = 200
                        resp.json.return_value = payload
                    return resp
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {}
            return resp

        mock_http_client.request = AsyncMock(side_effect=_request)

    @pytest.mark.asyncio
    async def test_collect_web_success_with_env_cookie(self, mock_http_client):
        """Web strategy standalone: cookie env + GetUsages counts (legacy shape)."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(),
            self._mock_settings(KIMI_AUTH_TOKEN="jwt_token_here"),
        ]
        self._http_router(mock_http_client, [("GetUsages", self.WEB_USAGES_RESPONSE)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 2
        assert any(card.get("window_type") == "weekly" for card in result)
        session = next(c for c in result if c.get("window_type") == "session")
        assert session["used_value"] == 47.0
        assert session["data_source"] == "web"
        weekly = next(c for c in result if c.get("window_type") == "weekly")
        assert "Weekly quota" in weekly["detail"]

    @pytest.mark.asyncio
    async def test_collect_api_key_success_pro_shape(self, mock_http_client):
        """API strategy: Pro plan Code API response -> session counts + monthly total.

        The Pro plan reports limit_month_code at 0 while limit_month_total has
        accrued usage — the vestigial code pool is suppressed (coding draws
        from the total pool), so no monthly "code" card."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(),
        ]
        self._http_router(mock_http_client, [("/coding/v1/usages", self.CODE_API_PRO_RESPONSE)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 2
        session = next(c for c in result if c.get("window_type") == "session")
        # Real counts win over the (stale, 0%) limit_5h ratio.
        assert session["used_value"] == 41.0
        assert session["limit_value"] == 100.0
        assert session["data_source"] == "api"
        assert session["input_source"] == "config"
        monthly_total = next(
            c for c in result if c.get("window_type") == "monthly" and c.get("variant") == "total"
        )
        assert monthly_total["pct_used"] == pytest.approx(1.1)
        assert monthly_total["used_value"] is None
        # Vestigial code pool: total > 0, code == 0 -> no "code" variant card.
        assert not any(
            c.get("window_type") == "monthly" and c.get("variant") == "code" for c in result
        )
        # Pro plan reports no weekly window via the Code API.
        assert not any(c.get("window_type") == "weekly" for c in result)

    @pytest.mark.asyncio
    async def test_collect_api_monthly_code_kept_on_fresh_month(self, mock_http_client):
        """Both pools at 0 (fresh month) is ambiguous — the code card stays."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(),
        ]
        payload = {
            "usages": {
                "limit_month_total": {"used_ratio": 0.0, "reset_time": "2026-10-19T00:00:00Z"},
                "limit_month_code": {"used_ratio": 0.0, "reset_time": "2026-10-19T00:00:00Z"},
            }
        }
        self._http_router(mock_http_client, [("/coding/v1/usages", payload)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        monthly_code = next(
            c for c in result if c.get("window_type") == "monthly" and c.get("variant") == "code"
        )
        assert monthly_code["pct_used"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_collect_api_monthly_code_kept_when_pool_accrues(self, mock_http_client):
        """A code pool that accrues its own usage is real — the card stays."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(),
        ]
        payload = {
            "usages": {
                "limit_month_total": {"used_ratio": 0.5, "reset_time": "2026-10-19T00:00:00Z"},
                "limit_month_code": {"used_ratio": 0.25, "reset_time": "2026-10-19T00:00:00Z"},
            }
        }
        self._http_router(mock_http_client, [("/coding/v1/usages", payload)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        monthly_code = next(
            c for c in result if c.get("window_type") == "monthly" and c.get("variant") == "code"
        )
        assert monthly_code["pct_used"] == pytest.approx(25.0)

    @pytest.mark.asyncio
    async def test_collect_api_me_enriches_identity_and_tier(self, mock_http_client):
        """GET /coding/v1/me provides email (account label), tier, and V2 gating:
        quota cards canonicalize onto the email account with no manual label."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(),
        ]
        self._http_router(
            mock_http_client,
            [
                ("/coding/v1/usages", self.CODE_API_PRO_RESPONSE),
                ("/coding/v1/me", self.CODE_API_ME_RESPONSE),
            ],
        )

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert collector.account_label == "user@kimi.test"
        assert all(c.get("account_label") == "user@kimi.test" for c in result)
        assert all(c.get("tier") == "Pro" for c in result)

    @pytest.mark.asyncio
    async def test_collect_api_me_does_not_clobber_user_label(self, mock_http_client):
        """A user-configured account label wins over the /me email."""
        collector = KimiCodingCollector(account_label="custom@example.com")
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(),
        ]
        self._http_router(
            mock_http_client,
            [
                ("/coding/v1/usages", self.CODE_API_PRO_RESPONSE),
                ("/coding/v1/me", self.CODE_API_ME_RESPONSE),
            ],
        )

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert collector.account_label == "custom@example.com"
        assert all(c.get("account_label") == "custom@example.com" for c in result)

    @pytest.mark.asyncio
    async def test_collect_api_me_v2_drops_legacy_weekly_counts(self, mock_http_client):
        """Legacy-shape responses still carry a vestigial weekly window on V2
        plans — /me goods_version=2 drops it."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(),
        ]
        payload = {
            **self.CODE_API_PRO_RESPONSE,
            "usage": {
                "limit": "100",
                "used": "3",
                "remaining": "97",
                "resetTime": "2026-09-25T13:52:05Z",
            },
        }
        self._http_router(
            mock_http_client,
            [
                ("/coding/v1/usages", payload),
                ("/coding/v1/me", self.CODE_API_ME_RESPONSE),
            ],
        )

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert not any(c.get("window_type") == "weekly" for c in result)

    @pytest.mark.asyncio
    async def test_collect_api_me_failure_is_non_fatal(self, mock_http_client):
        """A /me failure (404) must not break quota collection."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(),
        ]
        self._http_router(
            mock_http_client,
            [
                ("/coding/v1/usages", self.CODE_API_PRO_RESPONSE),
                ("/coding/v1/me", 404),
            ],
        )

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 2  # same as without /me: session + monthly total
        # No email identity leaked through (fallback label only).
        assert all("@" not in str(c.get("account_label") or "") for c in result)

    @pytest.mark.asyncio
    async def test_collect_api_key_401_falls_back_to_web_cookie(self, mock_http_client):
        """A rejected API key does not prevent a working web cookie from collecting quota."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="bad_key", session_cookie="jwt_cookie"),
            self._mock_settings(),
        ]
        self._http_router(
            mock_http_client,
            [
                ("/coding/v1/usages", 401),
                ("GetUsages", self.WEB_USAGES_RESPONSE),
                ("GetSubscriptionStats", self.WEB_STATS_RESPONSE),
                ("GetSubscription", self.WEB_SUBSCRIPTION_RESPONSE),
            ],
        )

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert result
        assert all(card.get("remaining") != "ERR" for card in result)
        assert any(card.get("data_source") == "web" for card in result)

    @pytest.mark.asyncio
    async def test_collect_both_kimi_strategies_auth_failed(self, mock_http_client):
        """When both configured Kimi credentials are rejected, surface auth failure."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="bad_key", session_cookie="bad_cookie"),
            self._mock_settings(),
        ]
        self._http_router(
            mock_http_client,
            [("/coding/v1/usages", 401), ("GetUsages", 401)],
        )

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert result[0]["error_type"] == "auth_failed"

    @pytest.mark.asyncio
    async def test_collect_api_key_401_without_fallback_returns_auth_failed(self, mock_http_client):
        """A rejected API key still surfaces auth failure when no web credential exists."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="bad_key"),
            self._mock_settings(),
        ]
        self._http_router(mock_http_client, [("/coding/v1/usages", 401)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert result[0]["error_type"] == "auth_failed"

    @pytest.mark.asyncio
    async def test_collect_merge_api_first_web_enriches(self, mock_http_client):
        """Default order: api is the base; web adds weekly + tier; counts stay authoritative."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(KIMI_AUTH_TOKEN="jwt_cookie"),
        ]
        self._http_router(
            mock_http_client,
            [
                ("/coding/v1/usages", self.CODE_API_PRO_RESPONSE),
                ("GetSubscriptionStats", self.WEB_STATS_RESPONSE),
                ("GetSubscription", self.WEB_SUBSCRIPTION_RESPONSE),
                ("GetUsages", self.WEB_USAGES_RESPONSE),
            ],
        )

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 4
        session = next(c for c in result if c.get("window_type") == "session")
        assert session["used_value"] == 41.0  # api counts, not web's 47
        weekly = next(c for c in result if c.get("window_type") == "weekly")
        assert weekly["pct_used"] == pytest.approx(0.38)  # web stats ratio, not detail counts
        assert weekly["data_source"] == "web"
        assert all(c.get("tier") == "Pro" for c in result)
        monthly = next(
            c for c in result if c.get("window_type") == "monthly" and c.get("variant") == "total"
        )
        assert monthly["pct_used"] == pytest.approx(1.1)  # base (api) monthly kept

    @pytest.mark.asyncio
    async def test_collect_merge_web_first_when_reordered(self, mock_http_client):
        """User reorders strategies: web becomes the base, api enriches (adds nothing new here)."""
        collector = KimiCodingCollector()
        collector.apply_strategy_config(
            [{"id": "web", "enabled": True}, {"id": "api", "enabled": True}]
        )
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(KIMI_AUTH_TOKEN="jwt_cookie"),
        ]
        self._http_router(
            mock_http_client,
            [
                ("/coding/v1/usages", self.CODE_API_PRO_RESPONSE),
                ("GetSubscriptionStats", self.WEB_STATS_RESPONSE),
                ("GetSubscription", self.WEB_SUBSCRIPTION_RESPONSE),
                ("GetUsages", self.WEB_USAGES_RESPONSE),
            ],
        )

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        session = next(c for c in result if c.get("window_type") == "session")
        assert session["used_value"] == 47.0  # web base counts now win
        assert session["data_source"] == "web"
        assert len(result) == 4  # api's monthly variants also present (same keys -> kept web's)

    @pytest.mark.asyncio
    async def test_collect_web_v2_goods_suppresses_inactive_windows(self, mock_http_client):
        """V2 goods (Pro): the web calls still report a vestigial weekly window
        (ratelimitCode7d) and a never-accruing code pool — both suppressed, so
        only session + monthly total render."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(),
            self._mock_settings(KIMI_AUTH_TOKEN="jwt_cookie"),
        ]
        v2_subscription = {
            "subscription": {
                "active": True,
                "status": "SUBSCRIPTION_STATUS_ACTIVE",
                "goods": {"title": "Pro", "version": "GOODS_VERSION_V2"},
            }
        }
        stats = {
            "ratelimitCode7d": {
                "ratio": 0.0038,
                "enabled": True,
                "resetTime": "2026-09-25T13:52:05Z",
            },
            "subscriptionBalance": {
                "amountUsedRatio": 0.0128,
                "kimiCodeUsedRatio": 0.0,
                "expireTime": "2026-10-19T00:00:00Z",
            },
        }
        self._http_router(
            mock_http_client,
            [
                ("GetUsages", self.WEB_USAGES_RESPONSE),
                ("GetSubscriptionStats", stats),
                ("GetSubscription", v2_subscription),
            ],
        )

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 2
        assert any(c.get("window_type") == "session" for c in result)
        monthly_total = next(
            c for c in result if c.get("window_type") == "monthly" and c.get("variant") == "total"
        )
        assert monthly_total["pct_used"] == pytest.approx(1.28)
        assert not any(c.get("window_type") == "weekly" for c in result)
        assert not any(
            c.get("window_type") == "monthly" and c.get("variant") == "code" for c in result
        )
        assert all(c.get("tier") == "Pro" for c in result)

    @pytest.mark.asyncio
    async def test_collect_web_v1_goods_keeps_weekly(self, mock_http_client):
        """V1 goods (legacy tiers): weekly is the primary quota — the card stays."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(),
            self._mock_settings(KIMI_AUTH_TOKEN="jwt_cookie"),
        ]
        v1_subscription = {
            "subscription": {
                "active": True,
                "status": "SUBSCRIPTION_STATUS_ACTIVE",
                "goods": {"title": "Allegretto", "version": "GOODS_VERSION_V1"},
            }
        }
        self._http_router(
            mock_http_client,
            [
                ("GetUsages", self.WEB_USAGES_RESPONSE),
                ("GetSubscriptionStats", self.WEB_STATS_RESPONSE),
                ("GetSubscription", v1_subscription),
            ],
        )

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        weekly = next(c for c in result if c.get("window_type") == "weekly")
        assert weekly["pct_used"] == pytest.approx(0.38)

    @pytest.mark.asyncio
    async def test_collect_cli_token_used_with_identity_headers(self, mock_http_client):
        """No API key: fresh CLI access token is used with X-Msh-* identity headers."""
        from app.services.credential_provider import CredentialMap

        future = datetime.now(UTC).timestamp() + 3600
        creds = CredentialMap(
            {"cli_access_token": "cli_access_token_123", "cli_expires_at": future},
            sources={"cli_access_token": "server"},
        )
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(creds=creds),
            self._mock_settings(),
        ]
        self._http_router(mock_http_client, [("/coding/v1/usages", self.CODE_API_PRO_RESPONSE)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        call = mock_http_client.request.call_args
        headers = call.kwargs["headers"]
        assert headers["Authorization"] == "Bearer cli_access_token_123"
        assert headers["X-Msh-Platform"] == "kimi_code_cli"
        # Deterministic uuid5 per account: no read of ~/.kimi-code/device_id.
        device_id = headers["X-Msh-Device-Id"]
        assert device_id == KimiCodingCollector()._kimi_code_identity_headers()["X-Msh-Device-Id"]
        assert len(device_id) == 32
        assert (
            device_id
            != KimiCodingCollector(account_id="other")._kimi_code_identity_headers()[
                "X-Msh-Device-Id"
            ]
        )
        assert len(result) == 2  # vestigial monthly "code" pool suppressed

    @pytest.mark.asyncio
    async def test_cli_sidecar_lookup_uses_credential_account_id(self):
        collector = KimiCodingCollector(account_id="alice@example.com")
        collector.credential_account_id = "default"
        future = datetime.now(UTC).timestamp() + 3600
        credential_patcher = self._patch_credentials()
        settings_patcher = self._mock_settings()
        try:
            with patch(
                "app.services.collectors.kimi_coding.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=(
                    {"cli_access_token": "cli-sidecar-token", "cli_expires_at": future},
                    {"source": "sidecar"},
                ),
            ) as get_with_metadata:
                resolved = await collector._resolve_cli_token()
        finally:
            credential_patcher.stop()
            settings_patcher.stop()

        assert resolved == ("cli-sidecar-token", "sidecar", None)
        get_with_metadata.assert_awaited_once_with("kimi_coding", account_id="default")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("cache_source", "expected_input_source"),
        [
            ("config", "config"),
            ("manual_config", "config"),
            ("sidecar-abc123", "sidecar"),
        ],
    )
    async def test_collect_api_key_from_token_cache(
        self, mock_http_client, cache_source, expected_input_source
    ):
        """Issue #343: a dashboard-pasted key mirrored into the ``api_key``
        cache slot resolves when the DB and env paths miss. Cache metadata
        drives input_source (config for UI pastes, sidecar otherwise), the
        lookup is identity-scoped, and an API key is not a CLI token — no
        X-Msh-* identity headers."""
        collector = KimiCodingCollector(account_id="alice@example.com")
        cache_patcher = patch(
            "app.services.collectors.kimi_coding.token_cache.get_with_metadata",
            new_callable=AsyncMock,
            return_value=(
                {"api_key": "sk-kimi-cache-123"},  # pragma: allowlist secret
                {"source": cache_source},
            ),
        )
        cache_lookup = cache_patcher.start()
        patchers = [
            self._patch_credentials(api_key=None),
            self._mock_settings(),
            cache_patcher,
        ]
        self._http_router(mock_http_client, [("/coding/v1/usages", self.CODE_API_PRO_RESPONSE)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        call = mock_http_client.request.call_args
        headers = call.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-kimi-cache-123"  # pragma: allowlist secret
        assert "X-Msh-Platform" not in headers
        assert "X-Msh-Device-Id" not in headers
        assert result
        assert all(c.get("input_source") == expected_input_source for c in result)

        # Identity-scoped lookup: never the "newest account" fallback that
        # get_with_metadata would use for account_id=None.
        assert cache_lookup.call_args.args[0] == "kimi_coding"
        assert cache_lookup.call_args.kwargs["account_id"] == "alice@example.com"

    @pytest.mark.asyncio
    async def test_cached_api_key_outranks_cli_token(self, mock_http_client):
        """A usable cached API key wins over a fresh CLI token (API-key class
        outranks the CLI tier, mirroring the DB-first precedence) — and the
        request must not carry CLI identity headers."""
        from app.services.credential_provider import CredentialMap

        future = datetime.now(UTC).timestamp() + 3600
        creds = CredentialMap(
            {"cli_access_token": "cli_access_token_123", "cli_expires_at": future},
            sources={"cli_access_token": "server"},
        )
        collector = KimiCodingCollector()
        with patch(
            "app.services.collectors.kimi_coding.token_cache.get_with_metadata",
            new_callable=AsyncMock,
            return_value=(
                {"api_key": "sk-kimi-cache-123"},  # pragma: allowlist secret
                {"source": "config"},
            ),
        ) as cache_lookup:
            patchers = [
                self._patch_credentials(creds=creds),
                self._mock_settings(),
            ]
            self._http_router(mock_http_client, [("/coding/v1/usages", self.CODE_API_PRO_RESPONSE)])
            try:
                result = await collector.collect(mock_http_client)
            finally:
                for p in patchers:
                    p.stop()

        assert cache_lookup.call_args.args[0] == "kimi_coding"
        assert cache_lookup.call_args.kwargs["account_id"] == "default"
        call = mock_http_client.request.call_args
        headers = call.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-kimi-cache-123"  # pragma: allowlist secret
        assert "X-Msh-Platform" not in headers
        assert result

    @pytest.mark.asyncio
    async def test_collect_stale_cli_token_falls_back_to_cookie(self, mock_http_client):
        """Expired CLI token is skipped; the cookie path takes over."""
        from app.services.credential_provider import CredentialMap

        stale = datetime.now(UTC).timestamp() - 10
        creds = CredentialMap(
            {"cli_access_token": "cli_access_token_old", "cli_expires_at": stale},
            sources={"cli_access_token": "server"},
        )
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(creds=creds),
            self._mock_settings(KIMI_AUTH_TOKEN="jwt_cookie"),
        ]
        self._http_router(mock_http_client, [("GetUsages", self.WEB_USAGES_RESPONSE)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        urls = [str(c.args[1]) for c in mock_http_client.request.call_args_list]
        assert not any("/coding/v1/usages" in u for u in urls)
        assert any("GetUsages" in u for u in urls)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_stale_cli_token_is_configured_with_relogin_hint(self, mock_http_client):
        """An expired CLI token keeps the card alive with a re-login hint."""
        from app.services.credential_provider import CredentialMap

        stale = datetime.now(UTC).timestamp() - 10
        creds = CredentialMap(
            {"cli_access_token": "cli_access_token_old", "cli_expires_at": stale},
            sources={"cli_access_token": "server"},
        )
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(creds=creds),
            self._mock_settings(),
        ]

        try:
            assert await collector.is_configured() is True
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert result[0]["error_type"] == "auth_failed"
        assert "CLI token expired" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_parse_reset_accepts_numeric_string_epoch(self, mock_http_client):
        """Reset values arriving as numeric strings parse as epoch seconds."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(),
        ]
        payload = {
            "usages": {
                "limit_month_total": {
                    "used_ratio": 0.5,
                    "reset_time": "1789742336",
                }
            }
        }
        self._http_router(mock_http_client, [("/coding/v1/usages", payload)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        monthly = next(c for c in result if c.get("window_type") == "monthly")
        assert monthly["reset"] != "Unknown"
        assert monthly["reset_at"] is not None

    @pytest.mark.asyncio
    async def test_membership_level_mapping_v1(self, mock_http_client):
        """user.membership.level maps to tier names for GOODS_VERSION_V1 goods."""
        collector = KimiCodingCollector()
        patchers = [
            self._patch_credentials(api_key="kimi_api_key"),
            self._mock_settings(),
        ]
        payload = {
            **self.CODE_API_PRO_RESPONSE,
            "usage": {
                "limit": "2048",
                "used": "214",
                "remaining": "1834",
                "resetTime": "2026-01-09T15:23:13Z",
            },
            "user": {"membership": {"level": "LEVEL_BASIC"}},
            "version": "GOODS_VERSION_V1",
        }
        self._http_router(mock_http_client, [("/coding/v1/usages", payload)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert all(c.get("tier") == "Moderato" for c in result)
        weekly = next(c for c in result if c.get("window_type") == "weekly")
        assert weekly["used_value"] == 214.0

    @pytest.mark.asyncio
    async def test_collect_empty_usage(self, mock_http_client):
        """Test Kimi Coding collection with empty usage response."""
        collector = KimiCodingCollector()

        patchers = [
            self._patch_credentials(),
            self._mock_settings(KIMI_AUTH_TOKEN="jwt_token_here"),
        ]
        self._http_router(mock_http_client, [("GetUsages", {})])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 1
        assert "No active plan" in result[0]["detail"]
        assert result[0]["remaining"] == "No active plan"

    @pytest.mark.asyncio
    async def test_collect_no_auth(self, mock_http_client):
        """Test Kimi Coding collection without any credential."""
        collector = KimiCodingCollector()

        patchers = [
            self._patch_credentials(),
            self._mock_settings(),
        ]

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert result[0]["error_type"] == "missing_config"

    @pytest.mark.asyncio
    async def test_collect_web_auth_error(self, mock_http_client):
        """Test Kimi Coding web collection with an expired/invalid cookie."""
        collector = KimiCodingCollector()

        patchers = [
            self._patch_credentials(),
            self._mock_settings(KIMI_AUTH_TOKEN="invalid_token"),
        ]
        self._http_router(mock_http_client, [("GetUsages", 401)])

        try:
            result = await collector.collect(mock_http_client)
        finally:
            for p in patchers:
                p.stop()

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert result[0]["error_type"] == "auth_failed"
