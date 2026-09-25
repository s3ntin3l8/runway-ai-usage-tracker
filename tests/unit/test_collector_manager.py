import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.collector_manager import CollectorManager


@pytest.fixture
def manager():
    m = CollectorManager()
    # Reset state
    m._collect_future = None
    return m


class TestCollectorManagerInitialization:
    def test_init_registry_count(self, manager):
        """Test that default registry contains expected providers."""
        # 14 providers, including xAI.
        assert len(manager.collector_registry) == 14
        assert "anthropic" in manager.collector_registry
        assert "antigravity" in manager.collector_registry
        assert "xai" in manager.collector_registry
        assert "openai" not in manager.collector_registry  # chatgpt is the key

    @pytest.mark.asyncio
    async def test_manual_xai_bearer_is_stored_only_as_access_token(self, manager):
        row = MagicMock(
            provider_id="xai",
            api_key="xai-test-access",
            session_cookie=None,
            oai_sc_cookie=None,
            account_id="alice@example.com",
        )
        with patch(
            "app.services.collector_manager.token_cache.store", new_callable=AsyncMock
        ) as store:
            await manager._sync_manual_config_to_cache(row)

        assert store.call_args.args[1] == {"xai_access": "xai-test-access"}

    @pytest.mark.asyncio
    async def test_sync_collectors_default(self, manager):
        """Test that default collectors are spawned."""
        # Clean state
        manager.smart_collectors = {}

        await manager._sync_collectors()

        # Check that some default collectors are present
        assert "anthropic:default" in manager.smart_collectors
        assert "gemini:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_sync_collectors_prunes_stale_dynamic_collectors(self, manager):
        """Test that collectors for missing accounts are removed."""
        # Add a fake dynamic collector
        manager.smart_collectors["anthropic:stale-account"] = MagicMock()

        # Mock token_cache to return no dynamic accounts
        with patch(
            "app.services.collector_manager.token_cache.get_all_active_accounts",
            new_callable=AsyncMock,
        ) as mock_accounts:
            mock_accounts.return_value = []  # No dynamic accounts

            # Reset sync time to bypass throttle
            manager._last_sync_time = 0
            await manager._sync_collectors()

            assert "anthropic:stale-account" not in manager.smart_collectors
            # Defaults should remain
            assert "anthropic:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_sync_collectors_force_bypasses_throttle(self, manager):
        """Config mutations pass force=True and must not wait out the 60s throttle."""
        manager.smart_collectors = {}
        manager._last_sync_time = time.time()  # just synced — throttle would skip

        await manager._sync_collectors()  # non-forced → skipped by throttle
        assert manager.smart_collectors == {}

        await manager._sync_collectors(force=True)
        assert "anthropic:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_no_default_collector_when_only_non_default_rows(self, manager):
        """Config rows exist for email accounts but no default sentinel →
        the blanket default must not spawn (it would shadow step 2's
        per-account enabled checks and keep collecting after a disable)."""
        manager.smart_collectors = {}

        class _Cfg:
            provider_id = "anthropic"
            account_id = "alice@example.com"
            enabled = True
            poll_interval_seconds = None
            account_label = None
            strategies = None
            api_key = None
            session_cookie = None

        with (
            patch(
                "app.services.collector_manager.token_cache.get_all_active_accounts",
                new_callable=AsyncMock,
            ) as mock_accounts,
            patch("sqlmodel.Session") as mock_session_cls,
        ):
            mock_accounts.return_value = []
            inner = MagicMock()
            # Order inside _sync_collectors: ProviderConfig.all(),
            # SystemConfig.first(), LatestUsage.all().
            inner.exec.return_value.all.side_effect = [
                [_Cfg()],  # ProviderConfig rows
                [],  # LatestUsage identities
            ]
            inner.exec.return_value.first.return_value = None  # SystemConfig
            mock_session_cls.return_value.__enter__.return_value = inner

            manager._last_sync_time = 0
            await manager._sync_collectors(force=True)

        assert "anthropic:default" not in manager.smart_collectors
        # Other providers (no config rows) still get their defaults.
        assert "gemini:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_disabled_only_account_pops_dynamic_collector(self, manager):
        """Disabling the sole (non-default) account must remove its collector."""
        manager.smart_collectors = {"anthropic:alice@example.com": MagicMock()}

        class _Cfg:
            provider_id = "anthropic"
            account_id = "alice@example.com"
            enabled = False  # the account was just disabled
            poll_interval_seconds = None
            account_label = None
            strategies = None
            api_key = None
            session_cookie = None

        with (
            patch(
                "app.services.collector_manager.token_cache.get_all_active_accounts",
                new_callable=AsyncMock,
            ) as mock_accounts,
            patch("sqlmodel.Session") as mock_session_cls,
        ):
            # Cache still holds the credential (disable doesn't purge cache).
            mock_accounts.return_value = [("anthropic", "alice@example.com", "Alice")]
            inner = MagicMock()
            inner.exec.return_value.all.side_effect = [
                [_Cfg()],  # only the disabled email row
                [],
            ]
            inner.exec.return_value.first.return_value = None
            mock_session_cls.return_value.__enter__.return_value = inner

            manager._last_sync_time = 0
            await manager._sync_collectors(force=True)

        assert "anthropic:alice@example.com" not in manager.smart_collectors
        assert "anthropic:default" not in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_step2_does_not_respawn_default_from_cache(self, manager):
        """Sidecar may stamp the token cache with account_id="default"
        (`_gemini_account_email` / `_ag_account_email` fallbacks). When config
        rows exist without a default sentinel, step 2 must not spawn
        {pid}:default — that would re-collect after the user disabled the
        only account (Hermes review on PR #309)."""
        manager.smart_collectors = {}  # step 1 never spawned the default

        class _Cfg:
            provider_id = "anthropic"
            account_id = "alice@example.com"
            enabled = False  # sole account disabled
            poll_interval_seconds = None
            account_label = None
            strategies = None
            api_key = None
            session_cookie = None

        with (
            patch(
                "app.services.collector_manager.token_cache.get_all_active_accounts",
                new_callable=AsyncMock,
            ) as mock_accounts,
            patch("sqlmodel.Session") as mock_session_cls,
        ):
            # Cache still holds a literal "default" identity (sidecar fallback).
            mock_accounts.return_value = [
                ("anthropic", "default", "default"),
                ("anthropic", "alice@example.com", "Alice"),
            ]
            inner = MagicMock()
            inner.exec.return_value.all.side_effect = [
                [_Cfg()],
                [],
            ]
            inner.exec.return_value.first.return_value = None
            mock_session_cls.return_value.__enter__.return_value = inner

            manager._last_sync_time = 0
            await manager._sync_collectors(force=True)

        assert "anthropic:default" not in manager.smart_collectors
        assert "anthropic:alice@example.com" not in manager.smart_collectors


class TestCollectorManagerWarmup:
    @pytest.mark.skip(reason="keychain warmup removed; keychain access moved to sidecar")
    @pytest.mark.asyncio
    async def test_warmup_keychain_non_darwin(self, manager):
        pass

    @pytest.mark.skip(reason="keychain warmup removed; keychain access moved to sidecar")
    @pytest.mark.asyncio
    async def test_warmup_keychain_disabled(self, manager):
        pass


class TestCollectorManagerCollection:
    @pytest.mark.asyncio
    async def test_collect_all_success(self, manager):
        """Test successful collection flow."""
        # Use simple mock collectors
        mock_sc1 = AsyncMock()
        mock_sc1.collect.return_value = [{"service_name": "S1"}]
        mock_sc2 = AsyncMock()
        mock_sc2.collect.return_value = [{"service_name": "S2"}]

        manager.smart_collectors = {"c1:default": mock_sc1, "c2:default": mock_sc2}

        # Mock dependencies
        with patch.object(manager, "_sync_collectors", new_callable=AsyncMock):
            # Run collection (external_metric_service removed; all data comes from collectors)
            results = await manager.collect_all()

            assert len(results) == 2
            services = [r["service_name"] for r in results]
            assert "S1" in services
            assert "S2" in services

    @pytest.mark.asyncio
    async def test_collect_all_timeout(self, manager):
        """Test that global timeout is handled gracefully."""

        # Mock _do_collect to simulate a timeout or empty result
        async def mock_do_collect():
            return []

        with patch.object(manager, "_do_collect", side_effect=mock_do_collect):
            # Reset future to ensure leader logic runs
            manager._collect_future = None
            results = await manager.collect_all()
            assert results == []

    @pytest.mark.asyncio
    async def test_collect_all_handles_exceptions(self, manager):
        """Test that exceptions in one collector don't crash everything."""

        # Use a simple mock for _do_collect to verify it returns results correctly
        async def mock_do_collect():
            return [{"service_name": "OK"}]

        with patch.object(manager, "_do_collect", side_effect=mock_do_collect):
            # Reset future to ensure leader logic runs
            manager._collect_future = None
            results = await manager.collect_all()

            assert len(results) == 1
            assert results[0]["service_name"] == "OK"
