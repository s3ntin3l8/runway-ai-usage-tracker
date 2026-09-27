"""Poller must evaluate credential-health alerts every cycle — even when
`collect_all()` returns no cards, since a fully broken credential often
means zero cards. See app.services.credential_alerts.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.poller import BackgroundPoller


@pytest.mark.asyncio
async def test_credential_alerts_checked_even_with_no_cards():
    p = BackgroundPoller(interval_seconds=900)

    with (
        patch("app.services.poller.manager") as mock_mgr,
        patch("app.services.poller.Session"),
        patch(
            "app.services.credential_alerts.check_credential_alerts", new=AsyncMock()
        ) as mock_check,
    ):
        mock_mgr.collect_all = AsyncMock(return_value=[])
        await p.poll_now()

    assert mock_check.called


@pytest.mark.asyncio
async def test_credential_alert_check_failure_is_non_fatal():
    p = BackgroundPoller(interval_seconds=900)

    with (
        patch("app.services.poller.manager") as mock_mgr,
        patch("app.services.poller.Session"),
        patch(
            "app.services.credential_alerts.check_credential_alerts",
            new=AsyncMock(side_effect=Exception("boom")),
        ),
    ):
        mock_mgr.collect_all = AsyncMock(return_value=[])
        await p.poll_now()  # must not raise
