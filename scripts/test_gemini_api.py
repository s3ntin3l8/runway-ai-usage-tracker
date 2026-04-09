import asyncio
import httpx
import json
import os
import time
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

CLIENT_ID = os.getenv("GEMINI_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GEMINI_OAUTH_CLIENT_SECRET", "")
CREDS_PATH = Path.home() / ".gemini" / "oauth_creds.json"


async def test_gemini_api():
    # Check if credentials are set
    if not CLIENT_ID or not CLIENT_SECRET:
        print(
            "ERROR: GEMINI_OAUTH_CLIENT_ID and GEMINI_OAUTH_CLIENT_SECRET must be set in environment"
        )
        return

    if not CREDS_PATH.exists():
        print(f"ERROR: Credentials not found at {CREDS_PATH}")
        return

    with open(CREDS_PATH, "r") as f:
        creds = json.load(f)

    print("--- Token Expiry Check ---")
    expiry = creds.get("expiry_date", 0)
    now = time.time() * 1000
    print(f"Expiry: {expiry}")
    print(f"Now:    {now}")

    async with httpx.AsyncClient() as client:
        if expiry < now:
            print("Token expired. Refreshing...")
            refresh_token = creds.get("refresh_token")
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            print(f"Refresh Response: {resp.status_code}")
            if resp.status_code == 200:
                new_data = resp.json()
                creds["access_token"] = new_data["access_token"]
                creds["expiry_date"] = int(time.time() * 1000) + (
                    new_data["expires_in"] * 1000
                )
                print("Token refreshed successfully.")
            else:
                print(f"Failed to refresh: {resp.text}")
                return

        token = creds.get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        print("\n--- Fetching Quota ---")
        quota_resp = await client.post(
            "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
            json={"project": ""},
            headers=headers,
        )
        print(f"Quota Status: {quota_resp.status_code}")
        if quota_resp.status_code == 200:
            print(json.dumps(quota_resp.json(), indent=2))
        else:
            print(quota_resp.text)

        print("\n--- Fetching Tier ---")
        tier_resp = await client.post(
            "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            json={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
            headers=headers,
        )
        print(f"Tier Status: {tier_resp.status_code}")
        if tier_resp.status_code == 200:
            print(json.dumps(tier_resp.json(), indent=2))
        else:
            print(tier_resp.text)


if __name__ == "__main__":
    asyncio.run(test_gemini_api())
