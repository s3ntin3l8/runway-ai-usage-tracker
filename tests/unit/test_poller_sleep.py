from collections import deque
from unittest.mock import AsyncMock, patch

import pytest

from app.services.poller import BackgroundPoller


def _make_cards(used: float, provider="anthropic", account="acc1"):
    return [{
        "service_name": "Test",
        "icon": "T",
        "remaining": "50%",
        "unit": "tokens",
        "reset": "monthly",
        "health": "good",
        "pace": "ok",
        "detail": "",
        "provider_id": provider,
        "account_id": account,
        "used_value": used,
        "limit_value": 1000.0,
        "data_source": "oauth",
    }]


@pytest.mark.asyncio
async def test_interval_unchanged_after_one_poll():
    """One poll with same values doesn't trigger sleep."""
    p = BackgroundPoller(interval_seconds=900)
    cards = _make_cards(100.0)
    with patch("app.services.poller.manager") as mock_mgr, \
         patch("app.services.poller.Session"):
        mock_mgr.collect_all = AsyncMock(return_value=cards)
        await p.poll_now()
    assert p._interval == 900


@pytest.mark.asyncio
async def test_sleep_triggered_after_3_identical_polls():
    """3 consecutive polls with identical values switches to 2-hour interval."""
    p = BackgroundPoller(interval_seconds=900)
    cards = _make_cards(100.0)
    with patch("app.services.poller.manager") as mock_mgr, \
         patch("app.services.poller.Session"):
        mock_mgr.collect_all = AsyncMock(return_value=cards)
        for _ in range(3):
            await p.poll_now()
    assert p._interval == 7200


@pytest.mark.asyncio
async def test_wake_on_changed_value():
    """Changing used_value resets interval to base after sleep."""
    p = BackgroundPoller(interval_seconds=900)
    p._interval = 7200  # simulate sleeping
    # Seed hash deques with old identical values
    p._snapshot_hashes["anthropic:acc1"] = deque([hash((100.0, 1000.0))] * 3, maxlen=3)

    changed_cards = _make_cards(500.0)  # different value
    with patch("app.services.poller.manager") as mock_mgr, \
         patch("app.services.poller.Session"):
        mock_mgr.collect_all = AsyncMock(return_value=changed_cards)
        await p.poll_now()
    assert p._interval == 900


@pytest.mark.asyncio
async def test_cached_cards_excluded_from_sleep_tracking():
    """Cards with data_source='cache' don't count toward dormancy."""
    p = BackgroundPoller(interval_seconds=900)
    cards = [{
        "service_name": "Test",
        "icon": "T",
        "remaining": "50%",
        "unit": "tokens",
        "reset": "monthly",
        "health": "good",
        "pace": "ok",
        "detail": "",
        "provider_id": "anthropic",
        "account_id": "acc1",
        "used_value": 100.0,
        "limit_value": 1000.0,
        "data_source": "cache",  # should be excluded
    }]
    with patch("app.services.poller.manager") as mock_mgr, \
         patch("app.services.poller.Session"):
        mock_mgr.collect_all = AsyncMock(return_value=cards)
        for _ in range(5):  # more than 3 polls
            await p.poll_now()
    assert "anthropic:acc1" not in p._snapshot_hashes
    assert p._interval == 900  # no sleep triggered


@pytest.mark.asyncio
async def test_all_accounts_must_be_dormant_for_sleep():
    """Sleep only triggers when ALL accounts are dormant."""
    p = BackgroundPoller(interval_seconds=900)
    # acc1 is stable, acc2 is always changing
    call_count = 0

    async def varying_collect():
        nonlocal call_count
        call_count += 1
        return _make_cards(100.0) + [{
            "service_name": "Test2",
            "icon": "T",
            "remaining": "50%",
            "unit": "tokens",
            "reset": "monthly",
            "health": "good",
            "pace": "ok",
            "detail": "",
            "provider_id": "openai",
            "account_id": "acc2",
            "used_value": float(call_count * 10),  # always different
            "limit_value": 1000.0,
            "data_source": "oauth",
        }]

    with patch("app.services.poller.manager") as mock_mgr, \
         patch("app.services.poller.Session"):
        mock_mgr.collect_all = varying_collect
        for _ in range(3):
            await p.poll_now()
    assert p._interval == 900  # NOT sleeping — acc2 is always changing
