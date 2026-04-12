"""
Unit tests for CollectorManager with Multi-Account support.
"""

import pytest
import asyncio
import platform
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.services.collector_manager import CollectorManager
from app.core.config import settings


@pytest.fixture
def manager():
    """Create a CollectorManager instance."""
    return CollectorManager()


class TestCollectorManagerInitialization:
    """Test initialization and dynamic spawning."""

    def test_init_registry_count(self, manager):
        """Test that manager initializes with correct number of registered providers."""
        assert len(manager.collector_registry) > 0
        assert manager.smart_collectors == {}
        assert manager._keychain_warmed_up is False

    @pytest.mark.asyncio
    async def test_sync_collectors_default(self, manager):
        """Test that default collectors are spawned on sync."""
        await manager._sync_collectors()
        # Should have at least the major providers (anthropic, gemini, etc)
        assert len(manager.smart_collectors) >= 6
        assert "anthropic:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_sync_collectors_prunes_stale_dynamic_collectors(self, manager):
        """Test that collectors for expired accounts are removed during sync."""
        manager.smart_collectors = {
            "anthropic:default": AsyncMock(),
            "github:active": AsyncMock(),
            "github:stale": AsyncMock(),
        }

        with patch(
            "app.services.collector_manager.token_cache.get_all_active_accounts",
            new_callable=AsyncMock,
            return_value=[("github", "active", "Active User")],
        ):
            await manager._sync_collectors()

        assert "anthropic:default" in manager.smart_collectors
        assert "github:active" in manager.smart_collectors
        assert "github:stale" not in manager.smart_collectors


class TestCollectorManagerWarmup:
    """Test keychain warmup logic."""

    @pytest.mark.asyncio
    async def test_warmup_keychain_non_darwin(self, manager):
        """Test that warmup is skipped on non-macOS platforms."""
        with patch("platform.system", return_value="Linux"):
            await manager._warmup_keychain()
            assert manager._keychain_warmed_up is True

    @pytest.mark.asyncio
    async def test_warmup_keychain_disabled(self, manager):
        """Test that warmup is skipped if disabled in settings."""
        with patch("platform.system", return_value="Darwin"):
            with patch.object(settings, "LOCAL_CREDENTIAL_SCRAPING_ENABLED", False):
                await manager._warmup_keychain()
                assert manager._keychain_warmed_up is True


class TestCollectorManagerCollection:
    """Test the main collect_all orchestration."""

    @pytest.mark.asyncio
    async def test_collect_all_success(self, manager):
        """Test successful collection from multiple sources."""
        # Mock SmartCollectors in a dict
        mock_smart1 = AsyncMock()
        mock_smart1.collect.return_value = [{"service_name": "S1", "remaining": "100%"}]
        mock_smart1.collector_name = "C1"
        
        mock_smart2 = AsyncMock()
        mock_smart2.collect.return_value = [{"service_name": "S2", "remaining": "50%"}]
        mock_smart2.collector_name = "C2"
        
        manager.smart_collectors = {
            "c1:default": mock_smart1,
            "c2:default": mock_smart2
        }
        
        # Patch sync_collectors to avoid overwriting our mocks
        with patch.object(manager, "_sync_collectors", new_callable=AsyncMock):
            # Mock external metrics
            with patch("app.services.collector_manager.external_metric_service.get_all_metrics", new_callable=AsyncMock) as mock_external:
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
        # Mock a slow collector
        async def slow_collect(*args, **kwargs):
            await asyncio.sleep(0.5)
            return []
            
        mock_smart = AsyncMock()
        mock_smart.collect.side_effect = slow_collect
        mock_smart.collector_name = "Slow"
        
        manager.smart_collectors = {"slow:default": mock_smart}
        
        with patch.object(manager, "_sync_collectors", new_callable=AsyncMock):
            # Run with very short timeout
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                results = await manager.collect_all()
                assert results == []

    @pytest.mark.asyncio
    async def test_collect_all_handles_exceptions(self, manager):
        """Test that exceptions in one collector don't crash everything."""
        mock_smart1 = AsyncMock()
        mock_smart1.collect.return_value = [{"service_name": "OK"}]
        
        mock_smart2 = AsyncMock()
        mock_smart2.collect.side_effect = Exception("Unexpected failure")
        mock_smart2.collector_name = "Failing"
        
        manager.smart_collectors = {
            "ok:default": mock_smart1,
            "fail:default": mock_smart2
        }
        
        with patch.object(manager, "_sync_collectors", new_callable=AsyncMock):
            with patch("app.services.collector_manager.external_metric_service.get_all_metrics", new_callable=AsyncMock) as mock_ext:
                mock_ext.return_value = []
                results = await manager.collect_all()
                
                # Should have the one successful result
                assert len(results) == 1
                assert results[0]["service_name"] == "OK"
