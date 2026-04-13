#!/usr/bin/env python3
"""
Quick test of the fixed GitHub collector.
"""

import asyncio
import os
import sys

# Add app to path
sys.path.insert(0, "/home/bjoern/projects/ai-usage-tracker")

import httpx

from app.services.collectors.github import GitHubCollector


async def test_collector():
    os.environ.setdefault(
        "GITHUB_TOKEN",
        "$(grep GITHUB_TOKEN /home/bjoern/projects/ai-usage-tracker/.env | cut -d'=' -f2)",
    )

    collector = GitHubCollector()
    async with httpx.AsyncClient(timeout=30.0) as client:
        cards = await collector.collect(client)

    print(f"Collected {len(cards)} cards:\n")
    for card in cards:
        print(f"Service: {card.get('service')}")
        print(f"  Remaining: {card.get('remaining')} {card.get('unit')}")
        print(f"  Reset: {card.get('reset')}")
        print(f"  Health: {card.get('health')}")
        print(f"  Detail: {card.get('detail')}")
        print()


if __name__ == "__main__":
    asyncio.run(test_collector())
