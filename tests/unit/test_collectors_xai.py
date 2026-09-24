"""Tests for the xAI token-status collector stub.

The xai collector doesn't fetch quota (xAI doesn't expose a programmatic
quota API — only ``/v1/api-key`` and ``/v1/models`` are reachable with a
bearer, and quota lives behind a Cloudflare-protected management console).
Instead it surfaces a single health signal: when the opencode-pushed
OAuth access JWT is expired, it emits an ``auth_required`` error card
pointing the user at the opencode CLI re-login flow.
"""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, patch

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


class TestExtractExpMs:
    def test_jwt_with_seconds_payload(self):
        """JWT `exp` in seconds gets multiplied up to ms."""
        exp_s = int(time.time()) + 3600
        jwt = _make_jwt(exp_s)
        got = XaiCollector._extract_exp_ms(jwt, None)
        assert got == exp_s * 1000

    def test_jwt_with_ms_payload(self):
        """A JWT `exp` already in ms (>= 1e12) is returned verbatim."""
        exp_ms = int((time.time() + 3600) * 1000)
        jwt = _make_jwt(exp_ms)
        got = XaiCollector._extract_exp_ms(jwt, None)
        assert got == exp_ms

    def test_explicit_expires_overrides_jwt(self):
        """The sidecar-stored ``expires`` (ms epoch) wins over JWT parsing."""
        jwt = _make_jwt(int(time.time()) + 3600)  # would say fresh
        got = XaiCollector._extract_exp_ms(jwt, str(int((time.time() - 86400) * 1000)))
        assert got == int((time.time() - 86400) * 1000)

    def test_no_jwt_no_exp_returns_none(self):
        assert XaiCollector._extract_exp_ms("not-a-jwt", None) is None


class TestGetXaiTokenStatus:
    @pytest.mark.asyncio
    async def test_expired_token_emits_auth_required_card(self):
        collector = XaiCollector(account_id="acc_test")
        expired = _make_jwt(int(time.time()) - 7200)

        async def fake_get(provider, account_id=None):
            # No xai_expires — let the JWT's exp claim drive the check.
            return {"xai_access": expired, "xai_refresh": "x"}

        with patch(
            "app.services.collectors.xai.token_cache.get",
            side_effect=fake_get,
        ):
            cards = await collector._get_xai_token_status(MagicMock())

        assert len(cards) == 1
        card = cards[0]
        assert card["error_type"] == "auth_failed"
        assert "expired" in card["detail"].lower()
        assert "opencode" in card["detail"].lower()
        assert collector._last_error_reason == "invalid_api_key"

    @pytest.mark.asyncio
    async def test_fresh_token_emits_no_cards(self):
        """A fresh access JWT is a healthy credential — no error card."""
        collector = XaiCollector(account_id="acc_test")
        fresh = _make_jwt(int(time.time()) + 86400)

        async def fake_get(provider, account_id=None):
            return {"xai_access": fresh, "xai_refresh": "x"}

        with patch(
            "app.services.collectors.xai.token_cache.get",
            side_effect=fake_get,
        ):
            cards = await collector._get_xai_token_status(MagicMock())

        assert cards == []
        assert collector._last_error_reason != "invalid_api_key"

    @pytest.mark.asyncio
    async def test_missing_token_returns_empty(self):
        """No credential at all — base collector's _error_handler takes over."""
        collector = XaiCollector(account_id="acc_test")

        with patch(
            "app.services.collectors.xai.token_cache.get",
            new_callable=AsyncMock,
            return_value={},
        ):
            cards = await collector._get_xai_token_status(MagicMock())

        assert cards == []


class TestIsConfigured:
    @pytest.mark.asyncio
    async def test_true_when_xai_access_present(self):
        collector = XaiCollector(account_id="acc_test")

        async def fake_get_token(provider, token_type, account_id=None):
            return "x" if token_type == "xai_access" else None

        with patch(
            "app.services.collectors.xai.token_cache.get_token",
            side_effect=fake_get_token,
        ):
            assert await collector.is_configured() is True

    @pytest.mark.asyncio
    async def test_false_when_no_xai_token(self):
        collector = XaiCollector(account_id="acc_test")
        with patch(
            "app.services.collectors.xai.token_cache.get_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            assert await collector.is_configured() is False


# Need MagicMock for the helper above
from unittest.mock import MagicMock  # noqa: E402
