import asyncio
import logging
from unittest.mock import patch

import httpx

from app.services.collectors.github import GitHubCollector


# Mock response for GitHub API
class MockResponse:
    def __init__(self, json_data, status_code=200):
        self.json_data = json_data
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self.json_data


async def test_github_label_override():
    # 1. Initialize avec an override label
    override_label = "Personal GitHub Account"
    collector = GitHubCollector(account_label=override_label)

    print(f"Initial account_label: {collector.account_label}")

    # Mock token
    with patch.object(GitHubCollector, "_get_token", return_value="fake_token"):
        # Mock HTTP requests to discover a DIFFERENT identity
        async def mock_request(*args, **kwargs):
            url = (
                args[1]
                if len(args) > 1
                else (args[0] if isinstance(args[0], str) else kwargs.get("url"))
            )
            if "/copilot_internal/user" in url:
                return MockResponse(
                    {
                        "quota_snapshots": [
                            {"metric": "chat", "remaining": 10, "entitlement": 100}
                        ],
                        "copilot_plan": "individual",
                    }
                )
            if "/user" in url and "/user/emails" not in url:
                return MockResponse(
                    {"login": "bjoern", "name": "Björn Hansen", "email": "real@email.com"}
                )
            if "/user/emails" in url:
                return MockResponse(
                    [{"email": "real@email.com", "primary": True, "verified": True}]
                )
            return MockResponse({}, 404)

        with (
            patch(
                "app.services.collectors.github.http_request_with_retry", side_effect=mock_request
            ),
            patch(
                "app.services.token_cache.token_cache.update_account_metadata", return_value=None
            ),
        ):
            # Run collection
            client = httpx.AsyncClient()
            results = await collector.collect(client)
            await client.aclose()

            print(f"Collector account_label after fetch: {collector.account_label}")

            # Check results
            if results:
                for card in results:
                    print(
                        f"Card: {card['service_name']} | Label: {card.get('account_label')} | Detail: {card['detail']}"
                    )

                    # Verify that the override label was preserved
                    assert card.get("account_label") == override_label
                    # Verify that the identity suffix was suppressed in detail
                    assert "real@email.com" not in card["detail"], (
                        f"Email found in detail: {card['detail']}"
                    )
            else:
                print("ERROR: No cards returned!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_github_label_override())
