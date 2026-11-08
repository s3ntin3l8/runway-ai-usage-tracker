"""Unit tests for web/interactive quota collectors (ChatGPT, OpenCode)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.collectors.chatgpt import ChatGPTCollector
from app.services.collectors.opencode import OpenCodeCollector


class TestChatGPTCollector:
    """Test suite for ChatGPT collector."""

    @pytest.mark.asyncio
    async def test_collect_api_success(self, mock_http_client, mock_chatgpt_usage_response):
        """Test successful ChatGPT API collection."""
        collector = ChatGPTCollector()

        # Mock Usage Info (Unified)
        usage_data = mock_chatgpt_usage_response.copy()
        usage_data["plan_type"] = "plus"

        usage_response = MagicMock(spec=httpx.Response)
        usage_response.status_code = 200
        usage_response.headers = {}
        usage_response.json.return_value = usage_data

        # Only one call needed now
        mock_http_client.request.side_effect = [usage_response]

        with patch(
            "app.services.credential_provider.CredentialProvider.get_chatgpt_data",
            return_value={"access_token": "test_token"},
        ):
            result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].get("variant") == "Codex"
        assert "PLUS" in str(result[0].get("detail", ""))
        assert "%" in str(result[0].get("remaining", ""))

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="browser-cookie / local fallback moved to sidecar")
    async def test_collect_fallback_to_local_logs(self, mock_http_client):
        """Test fallback to local logs when API fails."""
        collector = ChatGPTCollector()

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 500
        mock_http_client.get.return_value = mock_response

        with patch("app.services.collectors.chatgpt_oauth.settings") as mock_settings:
            with patch("builtins.open", side_effect=FileNotFoundError):
                result = await collector.collect(mock_http_client)

        # Should return error card if both API and logs fail
        assert isinstance(result, list)

    @pytest.mark.skip(reason="local strategy moved to sidecar")
    def test_chatgpt_codex_sums_all_messages(self, tmp_path):
        """_process_codex_sessions should sum usage from ALL interactions (Total Consumption)."""
        from app.services.collectors.chatgpt import ChatGPTCollector

        collector = ChatGPTCollector()

        # Two turns in one session.
        # msg1: input 100, output 50
        # msg2: input 120 (billed 120, not 20), output 60
        # Total Consumption should be: Input 220, Output 110, Total 330.

        now = datetime.now(UTC)
        reset_at = now + timedelta(days=5)
        ts_1 = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        ts_2 = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

        entries = [
            # Turn 1
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-4o"}, "timestamp": ts_1}),
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": ts_1,
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 50}},
                        "rate_limits": {
                            "primary": {"resets_at": reset_at.timestamp(), "window_minutes": 10080}
                        },
                    },
                }
            ),
            # Turn 2
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": ts_2,
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 120, "output_tokens": 60}},
                        "rate_limits": {
                            "primary": {"resets_at": reset_at.timestamp(), "window_minutes": 10080}
                        },
                    },
                }
            ),
        ]

        fpath = tmp_path / "codex-test.jsonl"
        fpath.write_text("\n".join(entries) + "\n")

        result = collector._process_codex_sessions([str(fpath)])
        assert len(result) == 1
        enriched = result[0]

        assert enriched["token_usage"]["input"] == 220
        assert enriched["token_usage"]["output"] == 110
        assert enriched["token_usage"]["total"] == 330
        assert "in:220, out:110" in enriched["_enrichment_detail"]

    @pytest.mark.skip(reason="local strategy moved to sidecar")
    def test_chatgpt_codex_rolls_forward_old_window(self, tmp_path):
        """_process_codex_sessions should roll forward an old resets_at to find the current window cutoff."""

        from app.services.collectors.chatgpt import ChatGPTCollector

        collector = ChatGPTCollector()

        now = datetime.now(UTC)
        # Log event is from 8 days ago.
        # old_reset is 7.5 days ago.
        # If we roll it forward 7 days (10080 min), the new reset is 0.5 days ago.
        # The cutoff is 7 days before that -> 7.5 days ago.
        # So an event from 8 days ago is BEFORE the cutoff (correctly filtered).
        old_reset = now - timedelta(days=7.5)
        ts_old = (now - timedelta(days=8)).isoformat().replace("+00:00", "Z")

        entries = [
            json.dumps(
                {"type": "turn_context", "payload": {"model": "gpt-4o"}, "timestamp": ts_old}
            ),
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": ts_old,
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 50}},
                        "rate_limits": {
                            "primary": {"resets_at": old_reset.timestamp(), "window_minutes": 10080}
                        },
                    },
                }
            ),
        ]

        fpath = tmp_path / "codex-test-old.jsonl"
        fpath.write_text("\n".join(entries) + "\n")

        # The old event should be filtered out because it belongs to a previous window
        result = collector._process_codex_sessions([str(fpath)])
        assert len(result) == 0

    @pytest.mark.skip(reason="local strategy moved to sidecar")
    def test_chatgpt_codex_uses_primary_metadata_cutoff(self, tmp_path):
        """_process_codex_sessions should use the primary metadata reset_at for the cutoff."""

        from app.services.collectors.chatgpt import ChatGPTCollector

        collector = ChatGPTCollector()
        now = datetime.now(UTC)

        # Set the primary reset to the future
        primary_reset = now + timedelta(days=2)
        collector._primary_reset_at = primary_reset

        # Log event is from 1 hour ago.
        # Primary reset is 2 days in the future.
        # Window is 7 days.
        # Cutoff is 5 days ago.
        # Event from 1 hour ago is AFTER cutoff (correctly included).
        ts_recent = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

        entries = [
            json.dumps(
                {"type": "turn_context", "payload": {"model": "gpt-4o"}, "timestamp": ts_recent}
            ),
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": ts_recent,
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 50}},
                        "rate_limits": {
                            # Note: The log file contains a VERY old resets_at that would incorrectly put the event in a past window
                            # if it were rolled forward using the 10080 min logic blindly without primary metadata.
                            "primary": {
                                "resets_at": (now - timedelta(days=20)).timestamp(),
                                "window_minutes": 10080,
                            }
                        },
                    },
                }
            ),
        ]

        fpath = tmp_path / "codex-test-primary.jsonl"
        fpath.write_text("\n".join(entries) + "\n")

        result = collector._process_codex_sessions([str(fpath)])
        assert len(result) == 1
        assert result[0]["token_usage"]["total"] == 150


class TestOpenCodeCollector:
    """Test suite for OpenCode collector (api/web strategies + identity pinning).

    Most of the opencode tests live in
    ``tests/unit/test_opencode_account_id_fix.py``; this module retains
    the base-class enrichment-merge coverage.
    """

    @pytest.mark.asyncio
    async def test_enrich_results_matches_by_service_name(self):
        """Enrichment suffix lands on the matching primary card; orphan is dropped."""
        collector = OpenCodeCollector()

        primary = [
            {
                "service_name": "OpenCode",
                "window_type": "session",
                "detail": "$1.00 used · Web API",
                "remaining": "$11.00",
            },
            {
                "service_name": "OpenCode",
                "window_type": "weekly",
                "detail": "$5.00 used · Web API",
                "remaining": "$25.00",
            },
        ]
        enrichment = [
            {
                "service_name": "OpenCode",
                "window_type": "session",
                "_enrichment_detail": "$1.00 | in:10,000",
            },
            {
                "service_name": "OpenCode",
                "window_type": "weekly",
                "_enrichment_detail": "$5.00 | in:50,000",
            },
            {
                "service_name": "OpenCode",
                "window_type": "monthly",
                "_enrichment_detail": "$8.00 | in:80,000",
            },
        ]

        result = collector._enrich_results(primary, enrichment)

        assert len(result) == 2
        fh = next(c for c in result if c["window_type"] == "session")
        assert "in:10,000" in fh["detail"]
        weekly = next(c for c in result if c["window_type"] == "weekly")
        assert "in:50,000" in weekly["detail"]

    @pytest.mark.asyncio
    async def test_enrich_results_no_primary_returns_empty(self):
        """Enrichment does not promote fallback when primary is empty."""
        collector = OpenCodeCollector()

        enrichment = [
            {
                "service_name": "OpenCode",
                "window_type": "session",
                "_enrichment_detail": "$0.00",
                "token_usage": {"input": 0, "output": 0, "total": 0},
                "msgs": 0,
            },
        ]

        result = collector._enrich_results([], enrichment)
        assert result == []

    @pytest.mark.asyncio
    async def test_enrich_results_error_enrichment_returns_primary(self):
        """Error-shaped or empty enrichment leaves primary untouched."""
        collector = OpenCodeCollector()

        primary = [
            {
                "service_name": "OpenCode",
                "window_type": "session",
                "detail": "$1.00 used",
                "remaining": "$11.00",
            }
        ]

        assert collector._enrich_results(primary, []) == primary
        assert collector._enrich_results(primary, [{"remaining": "ERR"}]) == primary

    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    async def test_negative_tokens_clamped_to_zero(self):
        """Negative raw token values are clamped to zero."""
        collector = OpenCodeCollector()

        html = (
            '$R[1]={id:"usg_NEG",workspaceID:"wrk_1",'
            'timeCreated:new Date("2026-04-20T10:00:00.000Z"),'
            'timeUpdated:new Date("2026-04-20T10:00:00.100Z"),'
            "timeDeleted:null,"
            'model:"claude-sonnet-4-6",provider:"anthropic",'
            "inputTokens:-1000,"
            "outputTokens:-200,"
            "reasoningTokens:-50,"
            "cacheReadTokens:-500,"
            "cacheWrite5mTokens:0,"
            "cacheWrite1hTokens:null,"
            "cost:-970321,"
            'keyID:"key_AAA",sessionID:"",'
            'enrichment:$R[2]={plan:"lite"}'
            "}"
        )

        records = collector._parse_usage_records(html)
        assert len(records) == 1
        r = records[0]
        assert r["input"] == 0
        assert r["output"] == 0
        assert r["reasoning"] == 0
        assert r["cache_read"] == 0
        assert r["cost_usd"] == 0.0

    @pytest.mark.skip(reason="local-db / browser-cookie fallback moved to sidecar")
    def test_free_api_cards_have_provider_id(self):
        """Free and API cards emitted by _build_free_api_card carry provider_id."""
        collector = OpenCodeCollector()

        free_card = collector._build_free_api_card(
            "free",
            {
                "cost": 0.0,
                "msgs": 5,
                "tokens": {"input": 1000, "output": 100, "reasoning": 0, "cache_read": 0},
                "by_model": {"m2.5-free": {"cost": 0.0, "msgs": 5}},
            },
            "wrk_123",
            "user@example.com",
            "2026-04-20T12:00:00+00:00",
        )
        assert free_card["provider_id"] == "opencode"

        api_card = collector._build_free_api_card(
            "api",
            {
                "cost": 0.30,
                "msgs": 3,
                "tokens": {"input": 5000, "output": 500, "reasoning": 0, "cache_read": 0},
                "by_model": {"gpt-4o": {"cost": 0.30, "msgs": 3}},
            },
            "wrk_123",
            "user@example.com",
            "2026-04-20T12:00:00+00:00",
        )
        assert api_card["provider_id"] == "opencode"
