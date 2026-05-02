"""Smoke tests for Gemini collectors."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.collectors.gemini import GeminiCollector


@pytest.mark.asyncio
async def test_gemini_api_no_token_returns_empty():
    """When no valid token is available, api strategy returns empty list."""
    collector = GeminiCollector()

    with patch.object(collector, "_get_valid_token", AsyncMock(return_value=None)):
        client = AsyncMock(spec=httpx.AsyncClient)
        result = await collector._collect_via_api(client)
    assert result == []


@pytest.mark.asyncio
async def test_gemini_api_success():
    """Valid API response produces quota cards."""
    collector = GeminiCollector()

    tier_resp = MagicMock()
    tier_resp.status_code = 200
    tier_resp.headers = {}
    tier_resp.json.return_value = {
        "currentTier": {"id": "standard-tier", "name": "Gemini Code Assist"},
        "cloudaicompanionProject": "test-project-123",
    }

    quota_resp = MagicMock()
    quota_resp.status_code = 200
    quota_resp.headers = {}
    quota_resp.json.return_value = {
        "buckets": [
            {
                "modelId": "gemini-2.5-flash",
                "remainingFraction": 0.8,
                "resetTime": "2026-05-01T00:00:00Z",
            }
        ]
    }

    with patch.object(collector, "_get_valid_token", AsyncMock(return_value="tok")):
        with patch(
            "app.services.collectors.gemini_api.http_request_with_retry",
            AsyncMock(side_effect=[tier_resp, quota_resp]),
        ):
            client = AsyncMock(spec=httpx.AsyncClient)
            result = await collector._collect_via_api(None)

            assert len(result) == 1
            assert result[0]["service_name"] == "Gemini Flash"
            assert result[0]["remaining"] == "20%"
    assert result[0]["data_source"] == "api"
    assert result[0]["model_id"] == "flash"


@pytest.mark.asyncio
async def test_gemini_local_no_dirs_returns_empty():
    """When Gemini session dirs don't exist, local strategy returns empty list."""
    collector = GeminiCollector()

    with patch(
        "app.services.collectors.gemini_local.is_local_collector_enabled", return_value=True
    ):
        with patch("os.path.isdir", return_value=False):
            result = await collector._collect_via_logs(None)

    assert result == []


@pytest.mark.asyncio
async def test_gemini_local_disabled_returns_empty():
    """When local collector is disabled, local strategy returns empty list."""
    collector = GeminiCollector()

    with patch(
        "app.services.collectors.gemini_local.is_local_collector_enabled", return_value=False
    ):
        result = await collector._collect_via_logs(None)

    assert result == []


class TestGeminiEnrichment:
    """Tests for Gemini reset_at-aware enrichment."""

    def test_capture_primary_metadata_extracts_reset_per_model(self):
        """_capture_primary_metadata should store reset_at keyed by model_id."""
        collector = GeminiCollector()
        primary = [
            {"model_id": "flash", "reset_at": "2026-05-01T00:00:00+00:00"},
            {"model_id": "pro", "reset_at": "2026-05-02T00:00:00+00:00"},
        ]
        collector._capture_primary_metadata(primary)

        assert hasattr(collector, "_window_resets")
        assert "flash" in collector._window_resets
        assert "pro" in collector._window_resets
        assert collector._window_resets["flash"] == datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        assert collector._window_resets["pro"] == datetime(2026, 5, 2, 0, 0, tzinfo=UTC)

    def test_process_sessions_sums_all_messages(self, tmp_path):
        """_process_sessions should sum usage from ALL messages (Total Consumption)."""
        collector = GeminiCollector()

        # Two messages in one session.
        # msg1: input 100, output 50 (Total 150)
        # msg2: input 120 (adds 20 new, but 120 billed), output 60
        # Total Consumption should be: Input 220, Output 110, Total 330.
        entries = [
            json.dumps(
                {
                    "type": "gemini",
                    "timestamp": "2026-04-30T10:00:00Z",
                    "model": "gemini-3.1-pro-preview",
                    "tokens": {"input": 100, "output": 50, "total": 150},
                }
            ),
            json.dumps(
                {
                    "type": "gemini",
                    "timestamp": "2026-04-30T10:05:00Z",
                    "model": "gemini-3.1-pro-preview",
                    "tokens": {"input": 120, "output": 60, "total": 180},
                }
            ),
        ]

        fpath = tmp_path / "session-test.jsonl"
        fpath.write_text("\n".join(entries) + "\n")

        totals = collector._process_sessions([str(fpath)])

        assert totals["input"] == 220
        assert totals["output"] == 110
        assert totals["total"] == 330
        assert totals["model_classes"]["pro"]["input"] == 220
        assert len(totals["messages"]) == 2

    @pytest.mark.asyncio
    async def test_local_enrichment_emits_per_model_dicts(self, tmp_path):
        """Local strategy should emit separate enrichment dict per model class."""
        collector = GeminiCollector()

        canned_totals = {
            "input": 300,
            "output": 150,
            "cached": 0,
            "thoughts": 0,
            "tool": 0,
            "total": 450,
            "session_count": 2,
            "by_model": {
                "gemini-2.5-flash": {
                    "msgs": 1,
                    "tokens": {"input": 100, "output": 50, "reasoning": 0, "cache_read": 0},
                },
                "gemini-2.5-pro": {
                    "msgs": 1,
                    "tokens": {"input": 200, "output": 100, "reasoning": 0, "cache_read": 0},
                },
            },
            "model_classes": {
                "flash": {
                    "input": 100,
                    "output": 50,
                    "reasoning": 0,
                    "cache_read": 0,
                    "total": 150,
                    "session_count": 1,
                },
                "pro": {
                    "input": 200,
                    "output": 100,
                    "reasoning": 0,
                    "cache_read": 0,
                    "total": 300,
                    "session_count": 1,
                },
            },
            "messages": [
                {
                    "timestamp": "2026-04-29T12:00:00.000Z",
                    "tokens": {"input": 100, "output": 50, "total": 150},
                    "model": "gemini-2.5-flash",
                    "model_class": "flash",
                },
                {
                    "timestamp": "2026-04-29T12:00:00.000Z",
                    "tokens": {"input": 200, "output": 100, "total": 300},
                    "model": "gemini-2.5-pro",
                    "model_class": "pro",
                },
            ],
        }

        with patch(
            "app.services.collectors.gemini_local.is_local_collector_enabled", return_value=True
        ):
            with patch("os.path.isdir", return_value=True):
                with patch(
                    "app.services.collectors.gemini_local.glob.glob", return_value=["dummy.jsonl"]
                ):
                    with patch.object(collector, "_process_sessions", return_value=canned_totals):
                        result = await collector._collect_via_logs(None)

        assert len(result) == 2  # flash + pro (no aggregate fallback anymore)

        by_model = {r["model_id"]: r for r in result}
        assert "flash" in by_model
        assert "pro" in by_model
        assert None not in by_model  # aggregate fallback removed

        assert by_model["flash"]["token_usage"]["input"] == 100
        assert by_model["pro"]["token_usage"]["input"] == 200

        assert by_model["flash"]["by_model"]["gemini-2.5-flash"]["tokens"]["input"] == 100
        assert by_model["flash"]["by_model"]["gemini-2.5-flash"]["tokens"]["output"] == 50
        assert by_model["flash"]["by_model"]["gemini-2.5-flash"]["tokens"]["total"] == 150
        assert by_model["pro"]["by_model"]["gemini-2.5-pro"]["tokens"]["input"] == 200
        assert by_model["pro"]["by_model"]["gemini-2.5-pro"]["tokens"]["output"] == 100
        assert by_model["pro"]["by_model"]["gemini-2.5-pro"]["tokens"]["total"] == 300

    @pytest.mark.asyncio
    async def test_local_enrichment_filters_by_reset_at(self, tmp_path):
        """Messages before the daily window start (reset_at - 24h) should be excluded."""
        collector = GeminiCollector()

        now = datetime.now(UTC)
        # reset_at is in the future (when the daily quota resets).
        # The 24h window started at reset_at - 24h.
        ts_before = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ts_after = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        reset_at = (now + timedelta(hours=1)).isoformat()

        # Set the reset boundary
        collector._window_resets = {"flash": datetime.fromisoformat(reset_at)}

        canned_totals = {
            "input": 1100,
            "output": 550,
            "cached": 0,
            "thoughts": 0,
            "tool": 0,
            "total": 1650,
            "session_count": 2,
            "by_model": {
                "gemini-2.5-flash": {
                    "msgs": 2,
                    "tokens": {"input": 1100, "output": 550, "reasoning": 0, "cache_read": 0},
                },
            },
            "model_classes": {
                "flash": {
                    "input": 1100,
                    "output": 550,
                    "reasoning": 0,
                    "cache_read": 0,
                    "total": 1650,
                    "session_count": 2,
                },
            },
            "messages": [
                {
                    "timestamp": ts_before,
                    "tokens": {"input": 1000, "output": 500, "total": 1500},
                    "model": "gemini-2.5-flash",
                    "model_class": "flash",
                },
                {
                    "timestamp": ts_after,
                    "tokens": {"input": 100, "output": 50, "total": 150},
                    "model": "gemini-2.5-flash",
                    "model_class": "flash",
                },
            ],
        }

        with patch(
            "app.services.collectors.gemini_local.is_local_collector_enabled", return_value=True
        ):
            with patch("os.path.isdir", return_value=True):
                with patch(
                    "app.services.collectors.gemini_local.glob.glob", return_value=["dummy.jsonl"]
                ):
                    with patch.object(collector, "_process_sessions", return_value=canned_totals):
                        result = await collector._collect_via_logs(None)

        by_model = {r["model_id"]: r for r in result}
        flash = by_model.get("flash")
        assert flash is not None
        # Should only count the post-reset message (100 input + 50 output)
        assert flash["token_usage"]["input"] == 100
        assert flash["token_usage"]["output"] == 50
        assert flash["msgs"] == 1

    @pytest.mark.asyncio
    async def test_local_enrichment_caps_far_future_reset_at(self, tmp_path):
        """When reset_at is far in the future, window_start must be capped."""
        collector = GeminiCollector()

        now = datetime.now(UTC)
        # reset_at is 25h in the future.  Without the cap, window_start
        # would be now+1h (future) and all messages would be excluded.
        ts_msg = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        reset_at = (now + timedelta(hours=25)).isoformat()

        collector._window_resets = {"flash": datetime.fromisoformat(reset_at)}

        canned_totals = {
            "input": 200,
            "output": 100,
            "cached": 0,
            "thoughts": 0,
            "tool": 0,
            "total": 300,
            "session_count": 1,
            "by_model": {
                "gemini-2.5-flash": {
                    "msgs": 1,
                    "tokens": {"input": 200, "output": 100, "reasoning": 0, "cache_read": 0},
                },
            },
            "model_classes": {
                "flash": {
                    "input": 200,
                    "output": 100,
                    "reasoning": 0,
                    "cache_read": 0,
                    "total": 300,
                    "session_count": 1,
                },
            },
            "messages": [
                {
                    "timestamp": ts_msg,
                    "tokens": {"input": 200, "output": 100, "total": 300},
                    "model": "gemini-2.5-flash",
                    "model_class": "flash",
                },
            ],
        }

        with patch(
            "app.services.collectors.gemini_local.is_local_collector_enabled", return_value=True
        ):
            with patch("os.path.isdir", return_value=True):
                with patch(
                    "app.services.collectors.gemini_local.glob.glob", return_value=["dummy.jsonl"]
                ):
                    with patch.object(collector, "_process_sessions", return_value=canned_totals):
                        result = await collector._collect_via_logs(None)

        by_model = {r["model_id"]: r for r in result}
        flash = by_model.get("flash")
        assert flash is not None
        # Message should be included because window_start is capped
        assert flash["token_usage"]["input"] == 200
        assert flash["msgs"] == 1

    def test_enrich_results_matches_by_model_id(self):
        """_enrich_results should match Gemini enrichment dicts to primary cards by model_id."""
        collector = GeminiCollector()

        primary = [
            {"service_name": "Gemini", "model_id": "flash", "detail": "20% used"},
            {"service_name": "Gemini", "model_id": "pro", "detail": "10% used"},
        ]
        enrichment = [
            {
                "service_name": "Gemini",
                "model_id": "flash",
                "_enrichment_detail": "in: 100 out: 50",
                "token_usage": {"input": 100, "output": 50, "total": 150},
                "msgs": 1,
            },
            {
                "service_name": "Gemini",
                "model_id": "pro",
                "_enrichment_detail": "in: 200 out: 100",
                "token_usage": {"input": 200, "output": 100, "total": 300},
                "msgs": 1,
            },
        ]

        result = collector._enrich_results(primary, enrichment)
        by_model = {r["model_id"]: r for r in result}

        assert "in: 100 out: 50" in by_model["flash"]["detail"]
        assert by_model["flash"]["token_usage"]["input"] == 100
        assert "in: 200 out: 100" in by_model["pro"]["detail"]
        assert by_model["pro"]["token_usage"]["input"] == 200

    def test_enrich_results_prevents_cross_pollination(self):
        """Specific model enrichment should NOT pollute other cards via generic fallbacks."""
        collector = GeminiCollector()

        primary = [
            {"service_name": "Gemini", "model_id": "flash", "detail": "20% used"},
            {"service_name": "Gemini", "model_id": "pro", "detail": "10% used"},
        ]
        # Only provide enrichment for "pro"
        enrichment = [
            {
                "service_name": "Gemini",
                "model_id": "pro",
                "_enrichment_detail": "in: 200 out: 100",
                "token_usage": {"input": 200, "output": 100, "total": 300},
                "msgs": 1,
            },
        ]

        result = collector._enrich_results(primary, enrichment)
        by_model = {r["model_id"]: r for r in result}

        # Pro should be enriched
        assert "in: 200 out: 100" in by_model["pro"]["detail"]
        assert by_model["pro"]["token_usage"]["input"] == 200

        # Flash should NOT be enriched with pro's data
        assert "in: 200 out: 100" not in by_model["flash"]["detail"]
        assert "token_usage" not in by_model["flash"]
