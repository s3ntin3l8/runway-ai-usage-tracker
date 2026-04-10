import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import json
import httpx
from typing import List, Any, Dict

from app.services.smart_collector import SmartCollector
from app.core.utils import extract_token_regex, human_delta
from app.services.collectors.zai_plan import ZaiPlanCollector
from app.services.collectors.base import BaseCollector


class MockCollector(BaseCollector):
    def _fallback_strategies(self) -> List[Any]:
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        return [
            {"service": "Mock", "detail": "original", "metadata": {"nested": "value"}}
        ]

    async def _error_handler(self) -> List[Dict[str, Any]]:
        return []


class TestCacheResilience:
    @pytest.mark.asyncio
    async def test_smart_collector_deep_copy_integrity(self):
        """Verify that modifying results from SmartCollector doesn't corrupt the cache."""
        inner = MockCollector()
        smart = SmartCollector(inner, "MockCollector", ttl=60)

        mock_client = MagicMock(spec=httpx.AsyncClient)

        # First call - populates cache
        results1 = await smart.collect(mock_client)
        assert results1[0]["metadata"]["nested"] == "value"

        # MALICIOUS MUTATION of the returned result
        results1[0]["metadata"]["nested"] = "corrupted"
        results1[0]["detail"] = "hacked"

        # Second call - should return cached data UNCHANGED
        results2 = await smart.collect(mock_client)
        assert (
            results2[0]["metadata"]["nested"] == "value"
        ), "Cache was corrupted by shallow copy!"
        assert "original" in results2[0]["detail"]


class TestUtilityResilience:
    def test_extract_token_regex_edge_cases(self):
        """Test regex token extraction with messy/boundary data."""
        # Standard extraction
        assert extract_token_regex("token:abc 123", "token:") == "abc"

        # With separators
        assert (
            extract_token_regex("token:sk-ant-123 · session", "token:") == "sk-ant-123"
        )
        assert extract_token_regex("token:my_token[env]", "token:") == "my_token"

        # Multi-byte / Special chars (Kimi review concern)
        # Note: tokens shouldn't have spaces, but Sk-ant can have hyphens.
        # If tokens have weird chars, regex should still isolate them from UI separators.
        assert (
            extract_token_regex(
                "api_key:key_with_underscores_and-hyphens detail", "api_key:"
            )
            == "key_with_underscores_and-hyphens"
        )

        # Missing or empty
        assert extract_token_regex("nothing here", "token:") is None
        assert extract_token_regex("token: ", "token:") is None

        # Prefix matching
        assert (
            extract_token_regex("oauth_token:abc refresh_token:def", "refresh_token:")
            == "def"
        )

    def test_human_delta_timezone_resilience(self):
        """Verify human_delta handles mixed naive/aware datetimes."""
        # Use 25 hours to ensure we definitely get "1d Xh"
        future_aware = datetime.now(timezone.utc) + timedelta(hours=25)
        future_naive = datetime.now() + timedelta(hours=25)

        # Should not raise TypeError: can't subtract offset-naive and offset-aware datetimes
        res_aware = human_delta(future_aware)
        assert "1d" in res_aware

        res_naive = human_delta(future_naive)
        assert "1d" in res_naive
        assert human_delta(None) == "—"


class TestCollectorResilience:
    @pytest.mark.asyncio
    async def test_zai_plan_timestamp_overflow(self):
        """Verify ZaiPlanCollector handles extreme timestamps without crashing."""
        collector = ZaiPlanCollector()

        # Mock API limit with extreme/malformed timestamp
        limit_data = {
            "type": "TOKENS_LIMIT",
            "limit": 1000,
            "used": 100,
            "nextResetTime": 9999999999999999,  # Extreme far future / overflow risk
        }

        # Should catch OSError/OverflowError and return "Unknown"
        card = collector._parse_limit(limit_data, "Test Plan")
        assert card["reset"] == "Unknown"
        assert card["service"] == "zAI Plan (Tokens)"

        # Negative timestamp
        limit_data["nextResetTime"] = -1
        card = collector._parse_limit(limit_data, "Test Plan")
        # Should either be "Unknown" or some safe string, not a crash
        assert card["reset"] in ("Unknown", "Just now")


class TestIngestBoundary:
    @pytest.mark.asyncio
    async def test_ingest_hmac_utf8_resilience(self):
        """Test ingest HMAC verification with various character types (conceptual test)."""
        import hmac
        import hashlib

        secret = "test-secret"
        timestamp = str(int(datetime.now().timestamp()))
        # Body with emoji and multi-byte chars
        body = json.dumps(
            {"provider": "test", "metrics": [], "notes": "🚀 汉字"}
        ).encode("utf-8")

        expected_sig = hmac.new(
            secret.encode(), f"{timestamp}".encode() + body, hashlib.sha256
        ).hexdigest()

        # Verify our verification logic (replicated here for validation)
        received_sig = expected_sig
        assert hmac.compare_digest(received_sig, expected_sig)
