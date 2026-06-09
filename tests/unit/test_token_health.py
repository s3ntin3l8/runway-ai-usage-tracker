"""Unit tests for TokenHealthService (Phase 4D)."""

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.token_health import TokenHealthService, _classify_status


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
        # Without is_opaque, it is unknown
        assert _classify_status(None, is_opaque=False) == "unknown"
        # With is_opaque, it is valid (READY)
        assert _classify_status(None, is_opaque=True) == "valid"

    def test_can_refresh_suppresses_24h_warning(self):
        """Short-lived tokens with a refresh_token are auto-rolled — don't warn."""
        exp = time.time() + 3600  # 1 hour left
        # Without refresh path → "expiring" under the 24h rule.
        assert _classify_status(exp, can_refresh=False) == "expiring"
        # With refresh path → "valid" because auto-refresher will roll it.
        assert _classify_status(exp, can_refresh=True) == "valid"

    def test_can_refresh_still_warns_when_imminent(self, monkeypatch):
        """If exp is inside the auto-refresh interval the next tick is too late."""
        from app.services import token_health

        monkeypatch.setattr(token_health.settings, "TOKEN_AUTO_REFRESH_INTERVAL_SECONDS", 300)
        exp = time.time() + 100  # 100s < 300s interval → imminent
        assert _classify_status(exp, can_refresh=True) == "expiring"

    def test_can_refresh_falls_back_when_auto_refresh_disabled(self, monkeypatch):
        """If the user turned off auto-refresh, behave like the legacy 24h rule."""
        from app.services import token_health

        monkeypatch.setattr(token_health.settings, "TOKEN_AUTO_REFRESH_ENABLED", False)
        exp = time.time() + 3600
        assert _classify_status(exp, can_refresh=True) == "expiring"


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
            patch("os.path.exists", return_value=False),
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
            patch("os.path.exists", return_value=False),
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
            patch("os.path.exists", return_value=False),
            _mock_no_db_configs(),
        ):
            result = await service.get_health()

        assert result[0]["status"] == "valid"
        assert result[0]["expires_at"] is None

    @pytest.mark.asyncio
    async def test_expired_unrefreshable_is_redundant_with_healthy_sibling(self):
        """An expired, unrefreshable token is flagged redundant when another
        credential for the same provider is healthy — so the dashboard banner
        can ignore it instead of crying wolf."""
        service = TokenHealthService()
        expired_jwt = _make_jwt(time.time() - 3600)
        valid_jwt = _make_jwt(time.time() + 86400 * 7)

        mock_stats = {
            "chatgpt": {
                "dead": {"tokens": ["oauth_token"], "account_label": None, "ttl_remaining": 600},
                "alive": {"tokens": ["oauth_token"], "account_label": None, "ttl_remaining": 600},
            }
        }

        async def fake_get(provider, acc_id):
            return {"oauth_token": expired_jwt if acc_id == "dead" else valid_jwt}

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(return_value=mock_stats),
            ),
            patch(
                "app.services.token_health.token_cache.get",
                new=AsyncMock(side_effect=fake_get),
            ),
            patch("os.path.exists", return_value=False),
            _mock_no_db_configs(),
        ):
            result = await service.get_health()

        by_acc = {r["account_id"]: r for r in result}
        assert by_acc["dead"]["status"] == "expired"
        assert by_acc["dead"]["redundant"] is True
        assert by_acc["alive"]["redundant"] is False

    @pytest.mark.asyncio
    async def test_expired_unrefreshable_not_redundant_without_healthy_sibling(self):
        """When every credential for the provider is dead, keep alarming."""
        service = TokenHealthService()
        expired_jwt = _make_jwt(time.time() - 3600)

        mock_stats = {
            "chatgpt": {
                "dead": {"tokens": ["oauth_token"], "account_label": None, "ttl_remaining": 600},
            }
        }

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(return_value=mock_stats),
            ),
            patch(
                "app.services.token_health.token_cache.get",
                new=AsyncMock(return_value={"oauth_token": expired_jwt}),
            ),
            patch("os.path.exists", return_value=False),
            _mock_no_db_configs(),
        ):
            result = await service.get_health()

        assert result[0]["status"] == "expired"
        assert result[0]["redundant"] is False

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
            patch("os.path.exists", return_value=False),
            patch("app.services.token_health.Session", return_value=mock_session),
        ):
            result = await service.get_health()

        assert len(result) == 1
        r = result[0]
        assert r["provider"] == "openai"
        assert r["account_id"] == "config"
        assert r["source"] == "config"
        assert r["token_types"] == ["api_key"]
        assert r["status"] == "valid"
        assert r["can_refresh"] is False
