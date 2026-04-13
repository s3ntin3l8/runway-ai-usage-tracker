"""
Extended Gemini API investigation script to find overusage/credits data.
Tests various endpoints and response scenarios.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

CLIENT_ID = os.getenv("GEMINI_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GEMINI_OAUTH_CLIENT_SECRET", "")
CREDS_PATH = Path.home() / ".gemini" / "oauth_creds.json"

# ANSI colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"


def print_section(title):
    """Print a formatted section header."""
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}{title}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}\n")


async def refresh_token(client, creds):
    """Refresh OAuth token if needed."""
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        print(f"{RED}ERROR: No refresh token available{RESET}")
        return None

    resp = await client.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )

    if resp.status_code != 200:
        print(f"{RED}Token refresh failed: {resp.status_code}{RESET}")
        return None

    new_data = resp.json()
    creds["access_token"] = new_data["access_token"]
    creds["expiry_date"] = int(time.time() * 1000) + (new_data["expires_in"] * 1000)
    print(f"{GREEN}Token refreshed{RESET}")
    return creds


async def test_quota_detailed(client, headers, project_id=""):
    """Test quota endpoint and print all fields."""
    print_section("RETRIEVE USER QUOTA - FULL RESPONSE")

    body = {"project": project_id} if project_id else {}

    resp = await client.post(
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        json=body,
        headers=headers,
    )

    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"\n{BOLD}Full Response Structure:{RESET}")
        print(json.dumps(data, indent=2))

        # Check for any non-bucket fields
        print(f"\n{BOLD}Top-level keys:{RESET}")
        for key in data.keys():
            print(f"  • {key}")
            if key != "buckets":
                print(f"    Value: {data[key]}")

        # Deep dive into bucket fields
        if data.get("buckets"):
            print(f"\n{BOLD}Bucket fields (first bucket):{RESET}")
            first_bucket = data["buckets"][0]
            for key, value in first_bucket.items():
                print(f"  • {key}: {value}")
    else:
        print(f"{RED}Error: {resp.text}{RESET}")


async def test_tier_detailed(client, headers):
    """Test tier endpoint and print all fields."""
    print_section("LOAD CODE ASSIST - FULL RESPONSE")

    resp = await client.post(
        "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
        json={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
        headers=headers,
    )

    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"\n{BOLD}Full Response Structure:{RESET}")
        print(json.dumps(data, indent=2))

        # Look for billing/credit/overusage fields
        print(f"\n{BOLD}Searching for billing/credit/overusage fields:{RESET}")
        billing_keywords = [
            "bill",
            "credit",
            "spend",
            "cost",
            "payment",
            "overage",
            "overus",
            "limit",
            "quota",
        ]

        def search_dict(d, path=""):
            """Recursively search dict for billing-related fields."""
            for key, value in d.items():
                current_path = f"{path}.{key}" if path else key
                # Check if key matches billing keywords
                if any(kw in key.lower() for kw in billing_keywords):
                    print(f"  {GREEN}• {current_path}: {value}{RESET}")

                # Recurse into nested dicts
                if isinstance(value, dict):
                    search_dict(value, current_path)
                elif isinstance(value, list) and value and isinstance(value[0], dict):
                    for i, item in enumerate(value):
                        search_dict(item, f"{current_path}[{i}]")

        search_dict(data)
    else:
        print(f"{RED}Error: {resp.text}{RESET}")


async def test_cloud_billing_api(client, headers):
    """Try to access cloud billing API to get spend data."""
    print_section("CLOUD BILLING API (EXPERIMENTAL)")

    # Try various billing endpoints
    endpoints = [
        "https://cloudbilling.googleapis.com/v1/billingAccounts",
        "https://cloudbilling.googleapis.com/v1/projects/climbing-engine-hczq7/billingInfo",
    ]

    for endpoint in endpoints:
        print(f"\n{BOLD}Testing: {endpoint}{RESET}")
        try:
            resp = await client.get(endpoint, headers=headers)
            print(f"Status: {resp.status_code}")
            if resp.status_code == 200:
                print(json.dumps(resp.json(), indent=2))
            else:
                print(f"Response: {resp.text[:200]}")
        except Exception as e:
            print(f"Error: {e}")


async def test_usage_api(client, headers):
    """Try to find a usage/metrics API endpoint."""
    print_section("USAGE/METRICS API (EXPERIMENTAL)")

    # Try monitoring API for usage metrics
    project_id = "climbing-engine-hczq7"
    endpoints = [
        f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries",
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUsage",
        "https://cloudcode-pa.googleapis.com/v1internal:getUsage",
    ]

    for endpoint in endpoints:
        print(f"\n{BOLD}Testing: {endpoint}{RESET}")
        try:
            if "timeSeries" in endpoint:
                resp = await client.get(endpoint, headers=headers)
            else:
                resp = await client.post(endpoint, json={}, headers=headers)
            print(f"Status: {resp.status_code}")
            if resp.status_code == 200:
                print(json.dumps(resp.json(), indent=2)[:500])
            else:
                print(f"Response: {resp.text[:200]}")
        except Exception as e:
            print(f"Error: {e}")


async def main():
    print_section("GEMINI OVERUSAGE/CREDITS INVESTIGATION")
    print(f"Credentials path: {CREDS_PATH}")

    if not CREDS_PATH.exists():
        print(f"{RED}ERROR: Credentials not found{RESET}")
        return

    if not CLIENT_ID or not CLIENT_SECRET:
        print(
            f"{RED}ERROR: GEMINI_OAUTH_CLIENT_ID and GEMINI_OAUTH_CLIENT_SECRET must be set{RESET}"
        )
        return

    with open(CREDS_PATH) as f:
        creds = json.load(f)

    async with httpx.AsyncClient() as client:
        # Check and refresh token if needed
        expiry = creds.get("expiry_date", 0)
        now = time.time() * 1000

        if expiry < now:
            creds = await refresh_token(client, creds)
            if not creds:
                return

        token = creds.get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        # Test all endpoints
        await test_quota_detailed(client, headers, "climbing-engine-hczq7")
        await test_tier_detailed(client, headers)
        await test_cloud_billing_api(client, headers)
        await test_usage_api(client, headers)


if __name__ == "__main__":
    asyncio.run(main())
