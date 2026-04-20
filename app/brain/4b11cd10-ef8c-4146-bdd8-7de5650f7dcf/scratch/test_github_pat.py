import asyncio
import json

import httpx


async def test_github_api(token):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "User-Agent": "Runway-Test-Script",
    }

    async with httpx.AsyncClient() as client:
        print("--- /user ---")
        user_resp = await client.get("https://api.github.com/user", headers=headers)
        if user_resp.status_code == 200:
            print(json.dumps(user_resp.json(), indent=2))
        else:
            print(f"Error: {user_resp.status_code} {user_resp.text}")

        print("\n--- /user/emails ---")
        emails_resp = await client.get("https://api.github.com/user/emails", headers=headers)
        if emails_resp.status_code == 200:
            print(json.dumps(emails_resp.json(), indent=2))
        else:
            print(f"Error: {emails_resp.status_code} {emails_resp.text}")


if __name__ == "__main__":
    PAT = "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"
    asyncio.run(test_github_api(PAT))
