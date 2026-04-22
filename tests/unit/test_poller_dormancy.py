"""Tests for poller dormancy hash identity (service_name:window_type included)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.poller import BackgroundPoller


def _card(
    used: float, service: str = "Claude", window: str = "weekly", provider: str = "anthropic"
):
    return {
        "service_name": service,
        "icon": "T",
        "remaining": "50%",
        "unit": "tokens",
        "reset": "monthly",
        "health": "good",
        "pace": "ok",
        "detail": "",
        "provider_id": provider,
        "account_id": "acc1",
        "used_value": used,
        "limit_value": 1000.0,
        "window_type": window,
        "data_source": "api",
    }


@pytest.mark.asyncio
async def test_two_windows_same_values_but_different_identity_stay_distinct():
    """Two windows with identical (used, limit) but different window_type don't
    produce the same hash, so only one being static doesn't make everything dormant.
    """
    p = BackgroundPoller(interval_seconds=900)

    async def varying_second_window():
        # session is always 100/1000; weekly changes each poll
        call_count = getattr(varying_second_window, "_count", 0)
        varying_second_window._count = call_count + 1
        return [
            _card(100.0, service="Claude Session", window="session"),
            _card(float(call_count * 10), service="Claude Weekly", window="weekly"),
        ]

    with patch("app.services.poller.manager") as mock_mgr, patch("app.services.poller.Session"):
        mock_mgr.collect_all = varying_second_window
        for _ in range(3):
            await p.poll_now()

    # weekly window changes every poll — should not sleep
    assert p._interval == 900


@pytest.mark.asyncio
async def test_dormancy_includes_window_identity_in_hash():
    """Three identical polls with same service+window+values triggers sleep."""
    p = BackgroundPoller(interval_seconds=900)
    cards = [
        _card(100.0, service="Claude Session", window="session"),
        _card(200.0, service="Claude Weekly", window="weekly"),
    ]

    with patch("app.services.poller.manager") as mock_mgr, patch("app.services.poller.Session"):
        mock_mgr.collect_all = AsyncMock(return_value=cards)
        for _ in range(3):
            await p.poll_now()

    assert p._interval == 7200  # dormant
