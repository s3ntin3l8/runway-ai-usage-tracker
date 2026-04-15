"""Tests for ExternalMetricService cross-sidecar antigravity deduplication."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.external_metrics import ExternalMetricService


def _make_service(metrics: dict) -> ExternalMetricService:
    """Create an ExternalMetricService with pre-loaded metrics (no disk I/O)."""
    svc = ExternalMetricService.__new__(ExternalMetricService)
    svc._lock = asyncio.Lock()
    svc.metrics = metrics
    svc._last_save_time = 0.0
    svc._pending_save = False
    return svc


def _ag_card(service_name: str, account_label: str | None, remaining: str = "75.0%") -> dict:
    return {
        "service_name": service_name,
        "icon": "🛸",
        "remaining": remaining,
        "unit": "capacity",
        "reset": "Dynamic",
        "pace": "Continuous",
        "health": "good",
        "detail": "Pro | test [LSP]",
        "data_source": "lsp",
        "provider_id": "antigravity",
        "account_label": account_label,
        "model_id": service_name,
        "used_value": 25.0,
        "limit_value": 100.0,
        "unit_type": "percent",
        "window_type": "session",
    }


@pytest.mark.asyncio
async def test_deduplicates_same_account_same_model():
    """Two sidecars reporting the same account/model → only the more recent card kept."""
    old_ts = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    new_ts = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()

    svc = _make_service(
        {
            "sidecar-old": {
                "timestamp": old_ts,
                "cards": [_ag_card("claude-sonnet-4-5", "user@example.com", "75.0%")],
            },
            "sidecar-new": {
                "timestamp": new_ts,
                "cards": [_ag_card("claude-sonnet-4-5", "user@example.com", "55.0%")],
            },
        }
    )

    result = await svc.get_all_metrics()

    ag = [c for c in result if "claude-sonnet-4-5" in c.get("service_name", "")]
    assert len(ag) == 1, f"Expected 1 card, got {len(ag)}"
    assert "55.0%" in ag[0]["remaining"], "Should keep newer sidecar's card"


@pytest.mark.asyncio
async def test_keeps_different_accounts_separate():
    """Two sidecars with different accounts → both cards kept."""
    ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

    svc = _make_service(
        {
            "sidecar-a": {
                "timestamp": ts,
                "cards": [_ag_card("claude-sonnet-4-5", "alice@example.com")],
            },
            "sidecar-b": {
                "timestamp": ts,
                "cards": [_ag_card("claude-sonnet-4-5", "bob@example.com")],
            },
        }
    )

    result = await svc.get_all_metrics()

    ag = [c for c in result if "claude-sonnet-4-5" in c.get("service_name", "")]
    assert len(ag) == 2, "Different accounts should not be merged"


@pytest.mark.asyncio
async def test_inherits_account_label_from_lsp_to_file_fallback():
    """File-fallback card (no account_label) inherits label from LSP card in same sidecar."""
    ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    old_ts = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()

    lsp_card = _ag_card("claude-sonnet-4-5", "user@example.com", "60.0%")
    file_card = {**_ag_card("claude-opus-4", None, "80.0%"), "data_source": "local"}

    svc = _make_service(
        {
            "sidecar-main": {"timestamp": ts, "cards": [lsp_card, file_card]},
            "sidecar-old": {
                "timestamp": old_ts,
                "cards": [_ag_card("claude-sonnet-4-5", "user@example.com", "30.0%")],
            },
        }
    )

    result = await svc.get_all_metrics()

    opus_cards = [c for c in result if "claude-opus-4" in c.get("service_name", "")]
    assert len(opus_cards) == 1
    assert opus_cards[0].get("account_label") == "user@example.com"


@pytest.mark.asyncio
async def test_non_antigravity_cards_pass_through_unaffected():
    """Cards from other providers are not affected by the antigravity deduplication."""
    ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

    other_card = {
        "service_name": "Some Other Provider",
        "icon": "🔵",
        "remaining": "100%",
        "unit": "capacity",
        "reset": "—",
        "pace": "N/A",
        "health": "good",
        "detail": "other [Sidecar]",
        "data_source": "sidecar",
        "provider_id": "other",
    }

    svc = _make_service(
        {
            "sidecar-x": {
                "timestamp": ts,
                "cards": [other_card],
            }
        }
    )

    result = await svc.get_all_metrics()

    assert len(result) == 1
    assert "Some Other Provider" in result[0]["service_name"]


@pytest.mark.asyncio
async def test_appends_time_str_to_service_name():
    """Deduplicated antigravity cards get '(time_str)' appended to service_name."""
    ts = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()

    svc = _make_service(
        {
            "sidecar-x": {
                "timestamp": ts,
                "cards": [_ag_card("claude-sonnet-4-5", "user@example.com")],
            }
        }
    )

    result = await svc.get_all_metrics()

    assert len(result) == 1
    assert "3m ago" in result[0]["service_name"]
