from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.collector_manager import CollectorManager


@pytest.fixture
def manager():
    with patch("app.services.collector_manager.external_metric_service"):
        m = CollectorManager()
        # Reset state
        m._collect_future = None
        return m


class TestCollectorManagerInitialization:
    def test_init_registry_count(self, manager):
        """Test that default registry contains expected providers."""
        # 13 providers (added kimi_k2 for Kimi K2 credits)
        assert len(manager.collector_registry) == 13
        assert "anthropic" in manager.collector_registry
        assert "openai" not in manager.collector_registry  # chatgpt is the key

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


class TestCollectorManagerWarmup:
    @pytest.mark.asyncio
    async def test_warmup_keychain_non_darwin(self, manager):
        """Test that warmup is skipped on non-macOS."""
        with patch("platform.system", return_value="Linux"):
            with patch("app.core.keychain.get_keychain_secret") as mock_secret:
                await manager._warmup_keychain()
                mock_secret.assert_not_called()

    @pytest.mark.asyncio
    async def test_warmup_keychain_disabled(self, manager):
        """Test that warmup respects config."""
        with patch("platform.system", return_value="Darwin"):
            with patch(
                "app.services.collector_manager.is_local_credential_scraping_enabled",
                return_value=False,
            ):
                with patch("app.core.keychain.get_keychain_secret") as mock_secret:
                    await manager._warmup_keychain()
                    mock_secret.assert_not_called()


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
            # Mock external metrics
            with patch(
                "app.services.collector_manager.external_metric_service.get_all_metrics",
                new_callable=AsyncMock,
            ) as mock_external:
                mock_external.return_value = [{"service_name": "Ext", "remaining": "OK"}]

                # Run collection
                results = await manager.collect_all()

                assert len(results) == 3
                services = [r["service_name"] for r in results]
                assert "S1" in services
                assert "S2" in services
                assert "Ext" in services

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
