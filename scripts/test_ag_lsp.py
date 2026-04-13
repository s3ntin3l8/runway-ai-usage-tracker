import asyncio
import logging

import httpx

from app.services.collectors.antigravity import AntigravityCollector


async def test_antigravity():
    logging.basicConfig(level=logging.DEBUG)
    collector = AntigravityCollector()
    async with httpx.AsyncClient(verify=False) as client:
        results = await collector.collect(client)
        print("\n--- Antigravity Results ---")
        for res in results:
            print(f"Service: {res.get('service')}")
            print(f"Remaining: {res.get('remaining')}")
            print(f"Detail: {res.get('detail')}")
            print(f"Data Source: {res.get('data_source')}")
            print("-" * 30)

if __name__ == "__main__":
    asyncio.run(test_antigravity())
