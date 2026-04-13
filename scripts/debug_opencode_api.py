#!/usr/bin/env python3
"""
Debug script to test OpenCode Go API and see actual responses.
"""

import asyncio
import os

import httpx


async def test_opencode_api():
    token = os.environ.get("OPENCODE_GO_API_KEY")
    if not token:
        print("ERROR: OPENCODE_GO_API_KEY not set")
        return

    print(f"Testing with token: {token[:15]}...{token[-4:]}")
    print("=" * 60)

    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Test the usage endpoint
        print("\n1. Testing /v1/user/usage")
        print("-" * 40)
        try:
            resp = await client.get("https://api.opencode.ai/v1/user/usage", headers=headers)
            print(f"Status: {resp.status_code}")
            print(f"Content-Type: {resp.headers.get('content-type', 'N/A')}")
            print(
                f"Response body (first 2000 chars):\n{resp.text[:2000] if resp.text else '(empty)'}"
            )

            # Try to parse as JSON
            if resp.text:
                try:
                    data = resp.json()
                    print(f"\nParsed JSON: {data}")
                except Exception as e:
                    print(f"\nJSON parse error: {e}")

        except Exception as e:
            print(f"Request error: {e}")

        # Test if the base URL works
        print("\n2. Testing base URL (api.opencode.ai)")
        print("-" * 40)
        try:
            resp = await client.get(
                "https://api.opencode.ai/", headers=headers, follow_redirects=True
            )
            print(f"Status: {resp.status_code}")
            print(f"Content-Type: {resp.headers.get('content-type', 'N/A')}")
            print(
                f"Response body (first 500 chars):\n{resp.text[:500] if resp.text else '(empty)'}"
            )
        except Exception as e:
            print(f"Request error: {e}")

        # Try alternative endpoint patterns
        print("\n3. Testing /v1/usage (alternative)")
        print("-" * 40)
        try:
            resp = await client.get("https://api.opencode.ai/v1/usage", headers=headers)
            print(f"Status: {resp.status_code}")
            print(
                f"Response body (first 500 chars):\n{resp.text[:500] if resp.text else '(empty)'}"
            )
        except Exception as e:
            print(f"Request error: {e}")

        # Try the user endpoint
        print("\n4. Testing /v1/user")
        print("-" * 40)
        try:
            resp = await client.get("https://api.opencode.ai/v1/user", headers=headers)
            print(f"Status: {resp.status_code}")
            print(
                f"Response body (first 500 chars):\n{resp.text[:500] if resp.text else '(empty)'}"
            )
        except Exception as e:
            print(f"Request error: {e}")

        # Try the opencode.ai base URL (new API location?)
        print("\n5. Testing opencode.ai/api/v1/user/usage")
        print("-" * 40)
        try:
            resp = await client.get("https://opencode.ai/api/v1/user/usage", headers=headers)
            print(f"Status: {resp.status_code}")
            print(f"Content-Type: {resp.headers.get('content-type', 'N/A')}")
            print(
                f"Response body (first 1000 chars):\n{resp.text[:1000] if resp.text else '(empty)'}"
            )
        except Exception as e:
            print(f"Request error: {e}")

        # Try zen API path
        print("\n6. Testing opencode.ai/zen/api/v1/user/usage")
        print("-" * 40)
        try:
            resp = await client.get("https://opencode.ai/zen/api/v1/user/usage", headers=headers)
            print(f"Status: {resp.status_code}")
            print(f"Content-Type: {resp.headers.get('content-type', 'N/A')}")
            print(
                f"Response body (first 1000 chars):\n{resp.text[:1000] if resp.text else '(empty)'}"
            )
        except Exception as e:
            print(f"Request error: {e}")

        # Try without /v1 prefix
        print("\n7. Testing opencode.ai/api/user/usage")
        print("-" * 40)
        try:
            resp = await client.get("https://opencode.ai/api/user/usage", headers=headers)
            print(f"Status: {resp.status_code}")
            print(f"Content-Type: {resp.headers.get('content-type', 'N/A')}")
            print(
                f"Response body (first 1000 chars):\n{resp.text[:1000] if resp.text else '(empty)'}"
            )
        except Exception as e:
            print(f"Request error: {e}")

    print("\n" + "=" * 60)
    print("Debug complete")


if __name__ == "__main__":
    asyncio.run(test_opencode_api())
