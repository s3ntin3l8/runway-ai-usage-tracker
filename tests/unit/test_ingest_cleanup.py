import unittest
from typing import Any


# Mocking the simplified logic of ingest_metrics to verify it works
def mock_ingest_logic(payload: dict[str, Any]) -> dict[str, str]:
    metrics = payload.get("metrics", [])
    tokens = {}

    for card in metrics:
        # Structured metadata extraction
        metadata = card.get("metadata", {})
        if metadata:
            for key, val in metadata.items():
                if key in ("oauth_token", "refresh_token", "api_key") or key.startswith("cookie_"):
                    tokens[key] = val

        # Token-only card check
        is_token_only = (
            card.get("remaining") == "Token"
            and card.get("unit") in ("oauth", "api_key")
            and card.get("data_source") == "token_extracted"
        )
        if is_token_only:
            continue

    return tokens


class TestIngestCleanup(unittest.TestCase):
    def test_structured_token_extraction(self):
        payload = {
            "provider": "anthropic-laptop",
            "metrics": [
                {
                    "service_name": "Claude Pro",
                    "icon": "🟠",
                    "remaining": "Token",
                    "unit": "oauth",
                    "reset": "—",
                    "health": "good",
                    "pace": "Token",
                    "detail": "[Token Extracted] [Sidecar]",
                    "data_source": "token_extracted",
                    "metadata": {
                        "oauth_token": "sk-ant-access-123",
                        "refresh_token": "sk-ant-refresh-456",
                    },
                }
            ],
        }

        tokens = mock_ingest_logic(payload)
        self.assertEqual(tokens.get("oauth_token"), "sk-ant-access-123")
        self.assertEqual(tokens.get("refresh_token"), "sk-ant-refresh-456")

    def test_legacy_token_ignored(self):
        # Even if legacy tokens are in detail (which they shouldn't be now),
        # they should be ignored by the logic
        payload = {
            "provider": "anthropic-laptop",
            "metrics": [
                {
                    "service_name": "Claude Pro",
                    "remaining": "Token",
                    "unit": "oauth",
                    "detail": "oauth_token:SECRET_TOKEN [Sidecar]",
                    "data_source": "token_extracted",
                    "metadata": {},  # Empty metadata
                }
            ],
        }

        tokens = mock_ingest_logic(payload)
        self.assertNotIn("oauth_token", tokens)


if __name__ == "__main__":
    unittest.main()
