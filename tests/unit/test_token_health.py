"""Unit tests for TokenHealthService (Phase 4D)."""

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.token_health import TokenHealthService, _classify_status

_REVOKED_KEY = "zk-revoked"  # pragma: allowlist secret
_FAKE_1 = "opaque-value-1"
_FAKE_2 = "opaque-value-2"
_FILE_TOKEN = "gho_file"
_FILE_VALUE = "from-db"
_CONFIG = "config"
_SOURCES = {"oauth_token": _CONFIG, "api_key": _CONFIG, "other": "sidecar"}


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
    async def test_keeps_healthy_aggregate_with_live_sidecar_source(self):
        """A sidecar source must not hide an existing healthy account row."""
        service = TokenHealthService()
        valid_jwt = _make_jwt(time.time() + 86400 * 7)
        mock_stats = {
            "chatgpt": {
                "alice@example.com": {
                    "tokens": ["oauth_token"],
                    "account_label": "Alice",
                    "ttl_remaining": 900,
                    "source": "server",
                }
            }
        }

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(return_value=mock_stats),
            ),
            patch(
                "app.services.token_health.token_cache.get",
                new=AsyncMock(return_value={"oauth_token": valid_jwt}),
            ),
            patch(
                "app.services.token_health.token_cache.get_source_candidates",
                new=AsyncMock(
                    return_value=[
                        {
                            "source_id": "sidecar:host:cookie",
                            "sidecar_id": "host",
                            "credential_origin": "cookie:browser",
                        }
                    ]
                ),
            ),
            patch("os.path.exists", return_value=False),
            _mock_no_db_configs(),
        ):
            result = await service.get_health()

        assert len(result) == 1
        assert result[0]["provider"] == "chatgpt"
        assert result[0]["account_id"] == "alice@example.com"
        assert result[0]["status"] == "valid"

    @pytest.mark.asyncio
    async def test_expired_sidecar_source_remains_visible_after_cache_expiry(self):
        from datetime import UTC, datetime, timedelta

        from app.models.db import CredentialSource, SidecarRegistry

        service = TokenHealthService()
        source = CredentialSource(
            provider_id="antigravity",
            account_id="default",
            source_id="sidecar:dev-01:oauth",
            source_type="file",
            source_label="oauth_creds.json",
            credential_origin="path:/home/user/.config/agy/oauth_creds.json",
            sidecar_id="dev-01",
            credential_expires_at=datetime.now(UTC) - timedelta(days=1),
            token_types_json='["oauth_token", "refresh_token"]',
        )
        sidecar = SidecarRegistry(sidecar_id="dev-01", hostname="dev-01")
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.side_effect = [
            [sidecar],
            [source],
            [],
        ]

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.token_health.token_cache.get_source_candidates",
                new=AsyncMock(return_value=[]),
            ),
            patch("app.services.token_health.Session", return_value=mock_session),
            patch(
                "app.services.token_health._collect_server_credentials",
                return_value={},
            ),
        ):
            result = await service.get_health()

        assert len(result) == 1
        assert result[0]["provider"] == "antigravity"
        assert result[0]["account_id"] == "default"
        assert result[0]["account_label"] == "Pending identity"
        assert result[0]["source_name"] == "dev-01"
        assert result[0]["identity_pending"] is True
        assert result[0]["status"] == "expired"
        assert result[0]["token_types"] == ["oauth_token", "refresh_token"]

    @pytest.mark.asyncio
    async def test_pending_claude_source_is_not_duplicated_by_durable_row(self):
        from app.models.db import CredentialSource, SidecarRegistry

        service = TokenHealthService()
        source_id = "sidecar:dev-01:path:/claude/.credentials.json"
        source = CredentialSource(
            provider_id="anthropic",
            account_id="default",
            source_id=source_id,
            source_type="file",
            source_label=".credentials.json",
            credential_origin="path:/claude/.credentials.json",
            sidecar_id="dev-01",
            token_types_json='["oauth_token"]',
        )
        sidecar = SidecarRegistry(sidecar_id="dev-01", hostname="dev-01")
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.side_effect = [[sidecar], [source], []]
        live = {
            "account_id": "default",
            "source_id": source_id,
            "tokens": {"oauth_token": "opaque-oauth"},
            "metadata": {"sidecar_id": "dev-01", "identity_pending": True},
            "ttl_remaining": 300,
        }

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.token_health.token_cache._get_source_credentials",
                new=AsyncMock(return_value=[live]),
            ),
            patch(
                "app.services.token_health.token_cache.get_source_candidates",
                new=AsyncMock(return_value=[live]),
            ),
            patch("app.services.token_health.Session", return_value=mock_session),
            patch(
                "app.services.token_health._collect_server_credentials",
                return_value={},
            ),
        ):
            result = await service.get_health()

        assert len(result) == 1
        assert result[0]["account_id"] == f"unassigned:{source_id}"
        assert result[0]["assignment_pending"] is True

    @pytest.mark.asyncio
    async def test_invalid_durable_token_types_fall_back_to_empty_list(self):
        from app.models.db import CredentialSource, SidecarRegistry

        service = TokenHealthService()
        source = CredentialSource(
            provider_id="antigravity",
            account_id="default",
            source_id="sidecar:dev-01:malformed",
            source_type="file",
            source_label="auth.json",
            credential_origin="path:/auth.json",
            sidecar_id="dev-01",
            token_types_json="not-json",
        )
        sidecar = SidecarRegistry(sidecar_id="dev-01", hostname="dev-01")
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.side_effect = [[sidecar], [source], []]

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.token_health.token_cache._get_source_credentials",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.token_health.token_cache.get_source_candidates",
                new=AsyncMock(return_value=[]),
            ),
            patch("app.services.token_health.Session", return_value=mock_session),
            patch(
                "app.services.token_health._collect_server_credentials",
                return_value={},
            ),
        ):
            result = await service.get_health()

        assert len(result) == 1
        assert result[0]["token_types"] == []
        assert result[0]["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_live_sidecar_source_uses_cached_token_expiry(self):
        from app.models.db import CredentialSource, SidecarRegistry

        service = TokenHealthService()
        source = CredentialSource(
            provider_id="antigravity",
            account_id="alice@example.com",
            source_id="sidecar:dev-01:oauth",
            source_type="file",
            source_label="oauth_creds.json",
            credential_origin="path:/home/user/.config/agy/oauth_creds.json",
            sidecar_id="dev-01",
        )
        sidecar = SidecarRegistry(sidecar_id="dev-01", hostname="dev-01")
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.side_effect = [[sidecar], [source], []]
        live_token = _make_jwt(time.time() + 86400 * 7)

        with (
            patch(
                "app.services.token_health.token_cache.get_all_stats",
                new=AsyncMock(
                    return_value={
                        "antigravity": {
                            "alice@example.com": {
                                "account_label": "Alice",
                                "ttl_remaining": 900,
                                "source": "dev-01",
                            }
                        }
                    }
                ),
            ),
            patch(
                "app.services.token_health.token_cache.get",
                new=AsyncMock(return_value={"oauth_token": live_token}),
            ),
            patch(
                "app.services.token_health.token_cache.get_source_candidates",
                new=AsyncMock(
                    return_value=[
                        {
                            "source_id": source.source_id,
                            "tokens": {"oauth_token": live_token},
                        }
                    ]
                ),
            ),
            patch("app.services.token_health.Session", return_value=mock_session),
            patch(
                "app.services.token_health._collect_server_credentials",
                return_value={},
            ),
        ):
            result = await service.get_health()

        assert len(result) == 1
        assert result[0]["account_id"] == "alice@example.com"
        assert result[0]["status"] == "valid"
        assert result[0]["token_types"] == ["oauth_token"]

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
        credential that account can fall back on (an identity-less cache entry)
        is healthy — so the dashboard banner
        can ignore it instead of crying wolf."""
        service = TokenHealthService()
        expired_jwt = _make_jwt(time.time() - 3600)
        valid_jwt = _make_jwt(time.time() + 86400 * 7)

        mock_stats = {
            "chatgpt": {
                "dead@x.com": {
                    "tokens": ["oauth_token"],
                    "account_label": None,
                    "ttl_remaining": 600,
                },
                "alive": {"tokens": ["oauth_token"], "account_label": None, "ttl_remaining": 600},
            }
        }

        async def fake_get(provider, acc_id):
            return {"oauth_token": expired_jwt if acc_id == "dead@x.com" else valid_jwt}

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
        assert by_acc["dead@x.com"]["status"] == "expired"
        assert by_acc["dead@x.com"]["redundant"] is True
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
        mock_cfg.account_id = "default"
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
        assert r["account_id"] == "config:default"
        assert r["removable"] is False
        assert r["source"] == "config"
        assert r["token_types"] == ["api_key"]
        assert r["status"] == "valid"
        assert r["can_refresh"] is False


class TestPerAccountAndInvalid:
    """Real TokenCache + registry (no hand-built stats) so id matching is exercised."""

    @staticmethod
    def _cache(entries):
        from app.services.token_cache import TokenCache

        cache = TokenCache()
        for provider, acc_id, tokens, meta in entries:
            cache.seed_sync(provider, acc_id, tokens, meta)
        return cache

    async def _health_list(self, service, server_creds):
        """`get_health` rows as a list (several rows can share the `server` id)."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = []
        with (
            patch("app.services.token_health.token_cache", self._cache([])),
            patch("app.services.token_health.Session", return_value=mock_session),
            patch(
                "app.services.token_health._collect_server_credentials",
                return_value=server_creds,
            ),
        ):
            return await service.get_health()

    @staticmethod
    async def _health(cache, *, configs=None, server_creds=None):
        service = TokenHealthService()
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = configs or []
        with (
            patch("app.services.token_health.token_cache", cache),
            patch("app.services.token_health.Session", return_value=mock_session),
            patch(
                "app.services.token_health._collect_server_credentials",
                return_value=server_creds or {},
            ),
        ):
            rows = await service.get_health()
        return {r["account_id"]: r for r in rows}

    @pytest.mark.asyncio
    async def test_pending_claude_sidecar_oauth_is_visible(self):
        from app.services.token_cache import TokenCache

        cache = TokenCache()
        await cache.store(
            "anthropic",
            {"oauth_token": "claude-access-token", "expiry_date": "1790784000000"},
            account_id=None,
            source_id="sidecar:host-a:path:/claude",
            source_metadata={
                "sidecar_id": "host-a",
                "identity_pending": True,
                "identity_hint": "alice@example.com",
            },
        )

        rows = await self._health(cache)

        pending = rows["unassigned:sidecar:host-a:path:/claude"]
        assert pending["assignment_pending"] is True
        assert pending["account_label"] == "Unassigned (alice@example.com)"
        assert pending["source"] == "host-a"
        assert pending["expires_at"] is not None
        assert pending["removable"] is False

    @pytest.mark.asyncio
    async def test_resolved_claude_sidecar_oauth_stays_on_canonical_health_row(self):
        from app.services.token_cache import TokenCache

        cache = TokenCache()
        await cache.store(
            "anthropic",
            {"oauth_token": "claude-assigned-token"},
            account_id="alice@example.com",
            source_id="sidecar:host-a:path:/claude",
            source_metadata={"sidecar_id": "host-a", "identity_pending": False},
        )
        await cache.store(
            "anthropic",
            {"api_key": "configured-key"},  # pragma: allowlist secret
            account_id="alice@example.com",
        )

        rows = await self._health(cache)

        assert list(rows) == ["alice@example.com"]
        assert set(rows["alice@example.com"]["token_types"]) == {"oauth_token", "api_key"}

    @pytest.mark.asyncio
    async def test_expired_account_not_hidden_by_other_accounts_healthy_token(self):
        cache = self._cache(
            [
                ("anthropic", "a@x.com", {"oauth_token": _make_jwt(time.time() - 60)}, {}),
                ("anthropic", "b@x.com", {"oauth_token": _make_jwt(time.time() + 86400 * 9)}, {}),
            ]
        )
        rows = await self._health(cache)
        assert rows["a@x.com"]["status"] == "expired"
        assert rows["a@x.com"]["redundant"] is False  # b's token can't serve account a
        assert rows["b@x.com"]["redundant"] is False

    @pytest.mark.asyncio
    async def test_identityless_healthy_entry_still_makes_expired_redundant(self):
        cache = self._cache(
            [
                ("anthropic", "a@x.com", {"oauth_token": _make_jwt(time.time() - 60)}, {}),
                (
                    "anthropic",
                    "opaque-hash-a",  # pragma: allowlist secret
                    {"oauth_token": _make_jwt(time.time() + 86400 * 9)},
                    {},
                ),
            ]
        )
        rows = await self._health(cache)
        assert rows["a@x.com"]["redundant"] is True

    @pytest.mark.asyncio
    async def test_other_hash_account_does_not_make_expired_hash_account_redundant(self):
        cache = self._cache(
            [
                ("openrouter", "hashaaaaaaaa", {"oauth_token": _make_jwt(time.time() - 60)}, {}),
                (
                    "openrouter",
                    "hashbbbbbbbb",
                    {"oauth_token": _make_jwt(time.time() + 86400 * 9)},
                    {},
                ),
            ]
        )
        rows = await self._health(cache)
        assert rows["hashaaaaaaaa"]["status"] == "expired"
        assert rows["hashaaaaaaaa"]["redundant"] is False

    @pytest.mark.asyncio
    async def test_healthy_server_row_does_not_mask_an_expired_identified_account(self):
        cache = self._cache(
            [("anthropic", "a@x.com", {"oauth_token": _make_jwt(time.time() - 60)}, {})]
        )
        healthy = {"anthropic": {"oauth_token": _make_jwt(time.time() + 86400 * 9)}}
        rows = await self._health(cache, server_creds=healthy)
        assert rows["server"]["status"] == "valid"
        assert rows["a@x.com"]["redundant"] is False

    @pytest.mark.asyncio
    async def test_dead_server_oauth_does_not_mark_a_sibling_api_key_expired(self):
        creds = {
            "anthropic": {
                "oauth_token": _make_jwt(time.time() - 60),
                "api_key": _FAKE_1,
            }
        }
        service = TokenHealthService()
        rows = await self._health_list(service, creds)
        by_types = {tuple(r["token_types"]): r["status"] for r in rows}
        assert by_types[("oauth_token",)] == "expired"
        assert by_types[("api_key",)] == "valid"

    @pytest.mark.asyncio
    async def test_default_row_is_not_flagged_when_provider_has_several_accounts(self):
        """A flag under one identified account does not reach an unscoped
        (`default`) config/server row."""
        from app.services import auth_failures

        cache = self._cache(
            [
                ("zai", "a@x.com", {"api_key": _FAKE_1}, {}),
                ("zai", "b@x.com", {"api_key": _FAKE_2}, {}),
            ]
        )
        cfg = MagicMock(provider_id="zai", account_id="default", account_label=None)
        cfg.api_key = "zk-default-cfg"  # pragma: allowlist secret
        cfg.session_cookie = None
        auth_failures.mark("zai", "a@x.com")
        rows = await self._health(cache, configs=[cfg])
        assert rows["a@x.com"]["status"] == "invalid"
        assert rows["b@x.com"]["status"] == "valid"
        assert rows["config:default"]["status"] == "valid"

    @pytest.mark.asyncio
    async def test_default_flag_does_not_paint_every_account_of_the_provider(self):
        from app.services import auth_failures

        cache = self._cache(
            [
                ("openrouter", "hashaaaaaaaa", {"api_key": _FAKE_1}, {}),
                ("openrouter", "hashbbbbbbbb", {"api_key": _FAKE_2}, {}),
            ]
        )
        auth_failures.mark("openrouter", "default")
        rows = await self._health(cache)
        assert rows["hashaaaaaaaa"]["status"] == "valid"
        assert rows["hashbbbbbbbb"]["status"] == "valid"

    @pytest.mark.asyncio
    async def test_default_flag_stays_on_default_rows_when_a_hash_row_coexists(self):
        from app.services import auth_failures

        cfg = MagicMock(provider_id="openrouter", account_id="default", account_label=None)
        cfg.api_key = "sk-or-cfg"  # pragma: allowlist secret
        cfg.session_cookie = None
        cache = self._cache([("openrouter", "hashaaaaaaaa", {"api_key": _FAKE_1}, {})])
        auth_failures.mark("openrouter", "default")
        rows = await self._health(cache, configs=[cfg])
        assert rows["config:default"]["status"] == "invalid"
        assert rows["hashaaaaaaaa"]["status"] == "valid"

    @pytest.mark.asyncio
    async def test_default_credential_flag_reaches_the_default_config_row(self):
        """A dashboard-pasted `default` key is flagged under `default` (the
        collector's credential_account_id), which matches its config row."""
        from app.services import auth_failures

        cfg = MagicMock(provider_id="zai", account_id="default", account_label=None)
        cfg.api_key = "zk-default-cfg"  # pragma: allowlist secret
        cfg.session_cookie = None
        auth_failures.mark("zai", "default")
        rows = await self._health(self._cache([]), configs=[cfg])
        assert rows["config:default"]["status"] == "invalid"

    @pytest.mark.asyncio
    async def test_default_row_is_not_flagged_by_another_identitys_rejection(self):
        from app.services import auth_failures

        cfg = MagicMock(provider_id="zai", account_id="default", account_label=None)
        cfg.api_key = "zk-default-cfg"  # pragma: allowlist secret
        cfg.session_cookie = None
        auth_failures.mark("zai", "work")
        rows = await self._health(
            self._cache([]),
            configs=[cfg],
            server_creds={"zai": {"api_key": _FAKE_2}},
        )
        assert rows["config:default"]["status"] == "valid"
        assert rows["server"]["status"] == "valid"

    @pytest.mark.asyncio
    async def test_assumed_valid_config_row_is_not_a_healthy_sibling(self):
        cache = self._cache(
            [("anthropic", "a@x.com", {"oauth_token": _make_jwt(time.time() - 60)}, {})]
        )
        cfg = MagicMock(provider_id="anthropic", account_id="default", account_label=None)
        cfg.api_key = "sk-ant-static"  # pragma: allowlist secret
        cfg.session_cookie = None
        rows = await self._health(cache, configs=[cfg])
        assert rows["config:default"]["status"] == "valid"
        assert rows["a@x.com"]["redundant"] is False

    @pytest.mark.asyncio
    async def test_auth_failure_flags_opaque_hash_keyed_entry_invalid(self):
        """Collector flags under "default"; the cache entry is keyed by a hash."""
        from app.services import auth_failures

        cache = self._cache(
            [
                ("zai", "opaque-hash-a", {"api_key": _REVOKED_KEY}, {"source": "sc1"})
            ]  # pragma: allowlist secret
        )
        assert (await self._health(cache))["opaque-hash-a"][
            "status"
        ] == "valid"  # pragma: allowlist secret

        auth_failures.mark("zai", None)
        rows = await self._health(cache)
        assert rows["opaque-hash-a"]["status"] == "invalid"

        auth_failures.clear("zai", "default")
        assert (await self._health(cache))["opaque-hash-a"]["status"] == "valid"

    @pytest.mark.asyncio
    async def test_auth_failure_for_other_identified_account_does_not_leak(self):
        from app.services import auth_failures

        cache = self._cache([("zai", "b@x.com", {"api_key": "zk-ok"}, {})])
        auth_failures.mark("zai", "a@x.com")
        assert (await self._health(cache))["b@x.com"]["status"] == "valid"

    @pytest.mark.asyncio
    async def test_auth_failure_does_not_leak_between_hash_keyed_accounts(self):
        """Fingerprint-keyed providers key several distinct accounts by opaque
        hashes; one rejected credential must not flag its healthy siblings."""
        from app.services import auth_failures

        cache = self._cache(
            [
                ("openrouter", "hashaaaaaaaa", {"api_key": _FAKE_1}, {}),
                ("openrouter", "hashbbbbbbbb", {"api_key": _FAKE_2}, {}),
            ]
        )
        auth_failures.mark("openrouter", "hashaaaaaaaa")
        rows = await self._health(cache)
        assert rows["hashaaaaaaaa"]["status"] == "invalid"
        assert rows["hashbbbbbbbb"]["status"] == "valid"

    @pytest.mark.asyncio
    async def test_dashboard_saved_key_is_not_relisted_as_server_credential(self):
        cfg = MagicMock(provider_id="zai", account_id="default", account_label=None)
        cfg.api_key = "zk-dashboard"  # pragma: allowlist secret
        cfg.session_cookie = None
        rows = await self._health(
            self._cache([]),
            configs=[cfg],
            server_creds={"zai": {"api_key": "zk-dashboard"}},  # pragma: allowlist secret
        )
        assert set(rows) == {"config:default"}

    @pytest.mark.asyncio
    async def test_auth_failure_flags_config_and_server_rows(self):
        from app.services import auth_failures

        cfg = MagicMock(provider_id="zai", account_id="work", account_label=None)
        cfg.api_key = "zk-config"  # pragma: allowlist secret
        cfg.session_cookie = None
        auth_failures.mark("zai", "work")
        rows = await self._health(
            self._cache([]),
            configs=[cfg],
            server_creds={"openrouter": {"api_key": "sk-or-env"}},  # pragma: allowlist secret
        )
        assert rows["config:work"]["status"] == "invalid"
        assert rows["server"]["status"] == "valid"  # openrouter was never flagged

        auth_failures.mark("openrouter", None)
        rows = await self._health(self._cache([]), server_creds={"openrouter": {"api_key": "x"}})
        assert rows["server"]["status"] == "invalid"

    @pytest.mark.asyncio
    async def test_server_credentials_listed_without_values(self):
        creds = {"zai": {"api_key": "zk-SECRET-VALUE"}}  # pragma: allowlist secret
        rows = await self._health(self._cache([]), server_creds=creds)
        row = rows["server"]
        assert row["provider"] == "zai"
        assert row["token_types"] == ["api_key"]
        assert row["source"] == "server"
        assert row["removable"] is False
        assert "zk-SECRET-VALUE" not in json.dumps(rows)

    @pytest.mark.asyncio
    async def test_server_oauth_file_gets_real_expiry(self):
        creds = {"anthropic": {"oauth_token": _make_jwt(time.time() - 60)}}
        rows = await self._health(self._cache([]), server_creds=creds)
        assert rows["server"]["status"] == "expired"
        assert rows["server"]["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_server_credential_already_in_cache_is_not_duplicated(self):
        cache = self._cache([("zai", "default", {"api_key": "zk-same"}, {"source": "config"})])
        rows = await self._health(
            cache,
            server_creds={"zai": {"api_key": "zk-same"}},  # pragma: allowlist secret
        )
        assert "server" not in rows

    @pytest.mark.asyncio
    async def test_two_config_accounts_get_distinct_rows_and_cookie_still_checked(self):
        a = MagicMock(provider_id="chatgpt", account_id="a", account_label="A")
        a.api_key = "k-shared"  # pragma: allowlist secret
        a.session_cookie = "c-a"
        b = MagicMock(provider_id="chatgpt", account_id="b", account_label="B")
        b.api_key = "k-b"  # pragma: allowlist secret
        b.session_cookie = None
        cache = self._cache([("chatgpt", "x", {"api_key": "k-shared"}, {})])  # dedups a's key
        rows = await self._health(cache, configs=[a, b])
        assert "config:a" not in rows  # deduped against the cache...
        assert rows["config-cookie:a"]["token_types"] == ["session_cookie"]  # ...cookie kept
        assert rows["config:b"]["account_label"] == "B"

    @pytest.mark.asyncio
    async def test_can_refresh_only_where_an_endpoint_exists(self):
        tokens = {"oauth_token": _make_jwt(time.time() + 1800), "refresh_token": "rt"}
        cache = self._cache(
            [
                ("antigravity", "a@x.com", dict(tokens), {}),
                ("anthropic", "b@x.com", dict(tokens), {}),
            ]
        )
        rows = await self._health(cache)
        assert rows["a@x.com"]["can_refresh"] is False
        assert rows["b@x.com"]["can_refresh"] is True
        # Still classified as auto-rolled (30 min > refresh interval): not "expiring"
        assert rows["a@x.com"]["status"] == "valid"

    @pytest.mark.asyncio
    async def test_xai_with_xai_refresh_can_refresh(self):
        exp = time.time() + 3600
        tokens = {"xai_access": _make_jwt(exp), "xai_refresh": "rt"}
        cache = self._cache([("xai", "alice@example.com", tokens, {})])
        rows = await self._health(cache)
        assert rows["alice@example.com"]["can_refresh"] is True
        assert rows["alice@example.com"]["status"] == "valid"
        assert rows["alice@example.com"]["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_delete_refuses_managed_credentials(self):
        from app.services.token_health import CredentialNotRemovableError

        cache = self._cache([("zai", "default", {"api_key": "k"}, {"source": "config"})])
        service = TokenHealthService()
        with patch("app.services.token_health.token_cache", cache):
            with pytest.raises(CredentialNotRemovableError):
                await service.delete_credential("zai", "server")
            with pytest.raises(CredentialNotRemovableError):
                await service.delete_credential("zai", "config:default")
            with pytest.raises(CredentialNotRemovableError):
                await service.delete_credential("zai", "default")  # config-sourced cache row
            assert await cache.get("zai", "default") is not None

    @pytest.mark.asyncio
    async def test_delete_removes_sidecar_entry_and_clears_flag(self):
        from app.services import auth_failures

        cache = self._cache([("zai", "opaque-hash-b", {"api_key": "k"}, {"source": "sc1"})])
        auth_failures.mark("zai", "opaque-hash-b")  # pragma: allowlist secret
        service = TokenHealthService()
        with patch("app.services.token_health.token_cache", cache):
            assert await service.delete_credential("zai", "opaque-hash-b") is True
        assert auth_failures.flagged_accounts("zai") == set()  # pragma: allowlist secret


class TestServerCredentialScan:
    def test_includes_runway_config_dir_files_but_not_other_sources(self):
        """github_oauth.json lives in Runway's own config dir, so it is labelled
        `config`; it must still be listed (it was the old `local-file` row)."""
        from app.services.credential_provider import CredentialMap
        from app.services.token_health import _scan_server_credentials

        def fake(provider_id: str, **_: object) -> CredentialMap:
            if provider_id == "github":
                return CredentialMap(
                    {"oauth_token": _FILE_TOKEN, "api_key": _FILE_VALUE, "other": "x"},
                    sources=_SOURCES,
                )
            return CredentialMap({})

        with patch(
            "app.services.token_health.CredentialProvider.get_credentials", side_effect=fake
        ):
            found = _scan_server_credentials()
        assert found["github"] == {"oauth_token": _FILE_TOKEN, "api_key": _FILE_VALUE}
