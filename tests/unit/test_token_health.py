"""Unit tests for TokenHealthService (Phase 4D)."""

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.token_health import TokenHealthService, _classify_status, _parse_jwt_exp


def _mock_no_db_configs():
    """Return a context manager patch that makes ProviderConfig return no rows."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.exec.return_value.all.return_value = []
    return patch("app.services.token_health.Session", return_value=mock_session)


def _make_jwt(exp: float) -> str:
    """Build a minimal (unsigned) JWT with a given exp claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_bytes = json.dumps({"exp": exp, "sub": "test"}).encode()
    payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


class TestParseJwtExp:
    def test_extracts_exp_from_valid_jwt(self):
        exp = time.time() + 3600
        token = _make_jwt(exp)
        result = _parse_jwt_exp(token)
        assert result is not None
        assert abs(result - exp) < 1

    def test_returns_none_for_malformed_token(self):
        assert _parse_jwt_exp("not.a.jwt.with.extra.parts") is None
        assert _parse_jwt_exp("onlyone") is None
        assert _parse_jwt_exp("") is None

    def test_returns_none_for_non_jwt_string(self):
        # A plain API key / opaque token
        assert _parse_jwt_exp("sk-ant-api03-abcdefg") is None

    def test_returns_none_when_exp_missing(self):
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{"sub":"test"}').rstrip(b"=").decode()
        token = f"{header}.{payload}.sig"
        assert _parse_jwt_exp(token) is None


class TestClassifyStatus:
    def test_valid_token(self):
        exp = time.time() + 86400 * 7  # 7 days from now
        assert _classify_status(exp) == "valid"

    def test_expiring_soon(self):
        exp = time.time() + 3600  # 1 hour — within 24h warning window
        assert _classify_status(exp) == "expiring"

    def test_expired_token(self):
        exp = time.time() - 60  # 1 minute ago
        assert _classify_status(exp) == "expired"

    def test_unknown_when_no_exp(self):
        assert _classify_status(None) == "unknown"


class TestTokenHealthService:
    @pytest.mark.asyncio
    async def test_returns_health_for_each_account(self):
        service = TokenHealthService()
        future_exp = time.time() + 86400 * 7
        valid_jwt = _make_jwt(future_exp)

        mock_stats = {
            "anthropic": {
                "acc1": {
                    "tokens": ["oauth_token"],
                    "account_label": "Alice",
                    "ttl_remaining": 1800,
                }
            }
        }
        mock_tokens = {"oauth_token": valid_jwt, "refresh_token": "rtoken"}

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(return_value=mock_stats),
            ),
            patch(
                "app.services.token_health.token_cache.get",
                new=AsyncMock(return_value=mock_tokens),
            ),
            _mock_no_db_configs(),
        ):
            result = await service.get_health()

        assert len(result) == 1
        r = result[0]
        assert r["provider"] == "anthropic"
        assert r["account_id"] == "acc1"
        assert r["account_label"] == "Alice"
        assert r["status"] == "valid"
        assert r["expires_at"] is not None
        assert r["can_refresh"] is True

    @pytest.mark.asyncio
    async def test_expired_token_status(self):
        service = TokenHealthService()
        past_exp = time.time() - 3600
        expired_jwt = _make_jwt(past_exp)

        mock_stats = {
            "gemini": {
                "acc2": {"tokens": ["oauth_token"], "account_label": None, "ttl_remaining": 600}
            }
        }
        mock_tokens = {"oauth_token": expired_jwt}

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(return_value=mock_stats),
            ),
            patch(
                "app.services.token_health.token_cache.get",
                new=AsyncMock(return_value=mock_tokens),
            ),
            _mock_no_db_configs(),
        ):
            result = await service.get_health()

        assert result[0]["status"] == "expired"
        assert result[0]["can_refresh"] is False

    @pytest.mark.asyncio
    async def test_opaque_token_is_unknown(self):
        service = TokenHealthService()

        mock_stats = {
            "github": {
                "acc3": {"tokens": ["api_key"], "account_label": "Bob", "ttl_remaining": 900}
            }
        }
        mock_tokens = {"api_key": "gho_sometokenvalue"}

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(return_value=mock_stats),
            ),
            patch(
                "app.services.token_health.token_cache.get",
                new=AsyncMock(return_value=mock_tokens),
            ),
            _mock_no_db_configs(),
        ):
            result = await service.get_health()

        assert result[0]["status"] == "unknown"
        assert result[0]["expires_at"] is None

    @pytest.mark.asyncio
    async def test_provider_config_api_key_appears_in_health(self):
        """API keys stored in ProviderConfig (Settings → Providers) show in Token Health."""
        service = TokenHealthService()

        mock_cfg = MagicMock()
        mock_cfg.provider_id = "openai"
        mock_cfg.account_label = "my-account"
        mock_cfg.api_key = "sk-proj-xxx"
        mock_cfg.session_cookie = None

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = [mock_cfg]

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.token_health.token_cache.get",
                new=AsyncMock(return_value={}),
            ),
            patch("app.services.token_health.Session", return_value=mock_session),
        ):
            result = await service.get_health()

        assert len(result) == 1
        r = result[0]
        assert r["provider"] == "openai"
        assert r["account_id"] == "config"
        assert r["source"] == "config"
        assert r["token_types"] == ["api_key"]
        assert r["status"] == "unknown"
        assert r["can_refresh"] is False
