#!/usr/bin/env python3
"""
Test the fixed OpenCode collector.
"""

import asyncio
import os
import sys

sys.path.insert(0, "/home/bjoern/projects/ai-usage-tracker")

import httpx
from app.services.collectors.opencode import OpenCodeCollector


async def test_collector():
    os.environ.setdefault(
        "OPENCODE_GO_API_KEY",
        "$(grep OPENCODE_GO_API_KEY /home/bjoern/projects/ai-usage-tracker/.env | cut -d'=' -f2)",
    )

    collector = OpenCodeCollector()
    async with httpx.AsyncClient(timeout=30.0) as client:
        cards = await collector.collect(client)

    print(f"Collected {len(cards)} cards:\n")
    for card in cards:
        print(f"Service: {card.get('service')}")
        print(f"  Remaining: {card.get('remaining')} {card.get('unit')}")
        print(f"  Reset: {card.get('reset')}")
        print(f"  Health: {card.get('health')}")
        print(f"  Pace: {card.get('pace')}")
        print(f"  Detail: {card.get('detail')}")
        print()


if __name__ == "__main__":
    asyncio.run(test_collector())
