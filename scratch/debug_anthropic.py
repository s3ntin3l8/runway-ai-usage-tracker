import asyncio
import logging
from app.services.collector_manager import manager
from app.core.db import get_session
from sqlmodel import Session, select
from app.models.db import ProviderConfig

logging.basicConfig(level=logging.INFO)

async def test_anthropic():
    print("Testing Anthropic (Claude) collection...")
    # Trigger a sync first
    await manager.sync_manual_configs()
    
    results = await manager.collect_provider("anthropic")
    for r in results:
        print(f"Result for {r.get('service_name')}: {r.get('remaining')} [Source: {r.get('data_source')}]")
        if r.get("remaining") == "ERR":
            print(f"Error Detail: {r.get('detail')}")

if __name__ == "__main__":
    asyncio.run(test_anthropic())
