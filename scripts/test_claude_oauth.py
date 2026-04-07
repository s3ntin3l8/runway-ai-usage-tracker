import asyncio
import httpx
import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from app.core.config import settings
from app.services.collectors.anthropic import AnthropicCollector

async def test_token_loading():
    print("--- Test Token Loading ---")
    token = settings.CLAUDE_CODE_OAUTH_TOKEN
    if token:
        print(f"Token found (starts with): {token[:15]}...")
    else:
        print("Token NOT found.")

async def test_parsing_logic():
    print("\n--- Test Parsing Logic ---")
    collector = AnthropicCollector()
    
    # Mock data based on doc
    mock_data = {
        "five_hour": {"utilization": 45.5, "resets_at": "2026-04-07T12:00:00Z"},
        "seven_day": {"utilization": 12.3, "resets_at": "2026-04-14T00:00:00Z"},
        "seven_day_sonnet": {"utilization": 88.0, "resets_at": "2026-04-14T00:00:00Z"},
        "extra_usage": {"utilization": 0.0, "resets_at": None}
    }
    
    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self.json_data = json_data
            self.status_code = status_code
        def json(self): return self.json_data

    class MockClient:
        async def get(self, url, headers, timeout):
            return MockResponse(mock_data)

    results = await collector._get_claude_oauth(MockClient(), "fake-token")
    
    for r in results:
        print(f"Service: {r['service']}")
        print(f"  Remaining: {r['remaining']}")
        print(f"  Reset: {r['reset']}")
        print(f"  Detail: {r['detail']}")
        print(f"  Health: {r['health']}")

async def main():
    await test_token_loading()
    await test_parsing_logic()

if __name__ == "__main__":
    asyncio.run(main())
