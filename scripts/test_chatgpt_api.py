#!/usr/bin/env python3
"""
Debug script to check ChatGPT API response and see available fields including tier info.
"""

import asyncio
import json
from pathlib import Path

import httpx


async def test_chatgpt_api():
    # Load token from auth file
    auth_path = Path.home() / ".codex" / "auth.json"

    if not auth_path.exists():
        print(f"ERROR: Auth file not found at {auth_path}")
        return

    try:
        with open(auth_path) as f:
            data = json.load(f)
            token = data.get("tokens", {}).get("access_token")
            if not token:
                print("ERROR: No access_token found in auth file")
                return
    except Exception as e:
        print(f"ERROR: Failed to read auth file: {e}")
        return

    print("=== ChatGPT API Debug ===\n")

    async with httpx.AsyncClient() as client:
        url = "https://chatgpt.com/backend-api/wham/usage"
        headers = {"Authorization": f"Bearer {token}"}

        print(f"Calling: {url}")
        print(f"Token (first 20 chars): {token[:20]}...\n")

        try:
            resp = await client.get(url, headers=headers, timeout=10)
            print(f"Status: {resp.status_code}")
            print("\n=== FULL JSON RESPONSE ===")

            if resp.status_code == 200:
                data = resp.json()
                print(json.dumps(data, indent=2))

                # Also print specific fields we're interested in
                print("\n=== EXTRACTED FIELDS ===")
                primary = data.get("primary", {})
                print(f"utilization_percent: {primary.get('utilization_percent')}")
                print(f"resets_at: {primary.get('resets_at')}")

                # Check for tier/subscription info
                if "tier" in data:
                    print(f"tier: {data.get('tier')}")
                if "subscription" in data:
                    print(f"subscription: {data.get('subscription')}")
                if "plan" in data:
                    print(f"plan: {data.get('plan')}")
                if "entitlement" in data:
                    print(f"entitlement: {data.get('entitlement')}")

                # Check for any other top-level keys that might be relevant
                print(f"\nAll top-level keys: {list(data.keys())}")

            else:
                print(f"Error response: {resp.text}")

        except Exception as e:
            print(f"ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(test_chatgpt_api())
