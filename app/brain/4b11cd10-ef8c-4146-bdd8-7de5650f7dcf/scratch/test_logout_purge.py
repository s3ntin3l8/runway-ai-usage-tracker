import asyncio
from unittest.mock import MagicMock, patch
from app.services.collector_manager import manager
from app.services.collectors.github import GitHubCollector
from app.services.credential_provider import CredentialProvider

from app.services.smart_collector import SmartCollector

async def test_purge():
    # 1. Warm up: ensure collectors are spawned and registry is ready
    print("Initializing collectors...")
    with patch.object(SmartCollector, "collect", return_value=[]):
        with patch.object(CredentialProvider, "get_github_data", return_value={"api_key": "mock-token"}):
             manager._last_sync_time = 0
             await manager.collect_all()

    # 2. Mock token presence and collect
    mock_cards = [{"service_name": "Copilot Test", "provider_id": "github"}]
    print("Collecting with mock token...")
    with patch.object(CredentialProvider, "get_github_data", return_value={"api_key": "mock-token"}):
        # We need httpx client mock just for collect_one signature
        with patch("httpx.AsyncClient", return_value=MagicMock()):
            with patch.object(GitHubCollector, "_strategy_api", return_value=mock_cards):
                await manager.collect_one("github")
                registry = manager.get_registry_snapshot()
                github_cards = [c for c in registry if c.get("provider_id") == "github"]
                print(f"Registry has {len(github_cards)} GitHub cards")
                assert len(github_cards) > 0

    # 4. Trigger collect_one("github") WITHOUT token - should return [] and purge registry
    print("Collecting after logout (no token)...")
    with patch.object(CredentialProvider, "get_github_data", return_value={}):
        await manager.collect_one("github")
    
    registry = manager.get_registry_snapshot()
    github_cards = [c for c in registry if c.get("provider_id") == "github"]
    print(f"Registry has {len(github_cards)} GitHub cards after purge")
    assert len(github_cards) == 0
    print("SUCCESS: GitHub cards purged from registry!")

if __name__ == "__main__":
    asyncio.run(test_purge())
