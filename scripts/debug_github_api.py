#!/usr/bin/env python3
"""
Debug script to test GitHub Copilot API endpoints and see actual responses.
"""

import asyncio
import os

import httpx


async def test_github_api():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN not set")
        return

    print(f"Testing with token: {token[:10]}...{token[-4:]}")
    print("=" * 60)

    headers = {
        "Authorization": f"token {token}",
        "X-GitHub-Api-Version": "2025-04-01",
        "Accept": "application/json",
        "Editor-Version": "vscode/1.96.2",
        "Editor-Plugin-Version": "copilot-chat/0.26.7",
        "User-Agent": "GitHubCopilotChat/0.26.7",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Test 1: Copilot token endpoint
        print("\n1. Testing /copilot_internal/v2/token")
        print("-" * 40)
        try:
            resp = await client.get(
                "https://api.github.com/copilot_internal/v2/token", headers=headers
            )
            print(f"Status: {resp.status_code}")
            print(f"Response headers: {dict(resp.headers)}")
            print(f"Response body:\n{resp.text[:2000] if resp.text else '(empty)'}")
        except Exception as e:
            print(f"Error: {e}")

        # Test 2: Copilot user endpoint
        print("\n2. Testing /copilot_internal/user")
        print("-" * 40)
        try:
            resp = await client.get("https://api.github.com/copilot_internal/user", headers=headers)
            print(f"Status: {resp.status_code}")
            print(f"Response headers: {dict(resp.headers)}")
            print(f"Response body:\n{resp.text[:2000] if resp.text else '(empty)'}")
        except Exception as e:
            print(f"Error: {e}")

        # Test 3: Standard rate limit (for comparison)
        print("\n3. Testing /rate_limit (standard API)")
        print("-" * 40)
        try:
            resp = await client.get(
                "https://api.github.com/rate_limit",
                headers={"Authorization": f"Bearer {token}"},
            )
            print(f"Status: {resp.status_code}")
            data = resp.json()
            core = data.get("resources", {}).get("core", {})
            print(f"Core rate limit: {core.get('remaining')}/{core.get('limit')}")
        except Exception as e:
            print(f"Error: {e}")

        # Test 4: Try with Bearer token format instead of token format
        print("\n4. Testing /copilot_internal/v2/token with Bearer format")
        print("-" * 40)
        try:
            bearer_headers = {**headers, "Authorization": f"Bearer {token}"}
            resp = await client.get(
                "https://api.github.com/copilot_internal/v2/token",
                headers=bearer_headers,
            )
            print(f"Status: {resp.status_code}")
            print(f"Response body:\n{resp.text[:2000] if resp.text else '(empty)'}")
        except Exception as e:
            print(f"Error: {e}")

        # Test 5: Try without VS Code headers (clean request)
        print("\n5. Testing /copilot_internal/v2/token (minimal headers)")
        print("-" * 40)
        try:
            minimal_headers = {
                "Authorization": f"token {token}",
                "Accept": "application/json",
            }
            resp = await client.get(
                "https://api.github.com/copilot_internal/v2/token",
                headers=minimal_headers,
            )
            print(f"Status: {resp.status_code}")
            print(f"Response body:\n{resp.text[:2000] if resp.text else '(empty)'}")
        except Exception as e:
            print(f"Error: {e}")

        # Test 6: Check what scopes the token has
        print("\n6. Testing token scopes via /user")
        print("-" * 40)
        try:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}"},
            )
            print(f"Status: {resp.status_code}")
            print(f"X-OAuth-Scopes: {resp.headers.get('x-oauth-scopes', 'N/A')}")
            user_data = resp.json()
            print(f"User: {user_data.get('login')}")
            print(f"Plan: {user_data.get('plan', {}).get('name', 'N/A')}")
        except Exception as e:
            print(f"Error: {e}")

    print("\n" + "=" * 60)
    print("Debug complete")


if __name__ == "__main__":
    asyncio.run(test_github_api())
