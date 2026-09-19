"""Regression coverage for the opencode collector's ``account_id`` discovery
bug (issue #276).

The server-side opencode collector discovered the user's email from the
workspace HTML response and set ``self.account_label`` to it — but never
``self.account_id``. Result: events and cards streamed into
``account_id="default"`` even when a real identity was discoverable.

The fix mirrors the github collector pattern at
``app/services/collectors/github.py``: after discovering an email, also
stamp ``self.account_id`` (via ``normalize_account_id``) when the collector
is still on its default / unset identity.

These tests call the real collector methods (``_get_workspace_id`` and
``_parse_usage_data``) with mocked HTTP — same pattern as
``tests/unit/test_collectors.py:2553``. A regression that reverts
``opencode.py`` to the pre-fix behaviour fails them.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.collectors.opencode import OpenCodeCollector


def _workspace_response(email: str, workspace_id: str = "wrk_test") -> httpx.Response:
    """Minimal ``opencode.ai/_server`` response carrying an email + workspace id."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.text = f'someJS({{id:"{workspace_id}",name:"Test"}}); {email} '
    return resp


def _subscription_text(email: str) -> str:
    """Minimal ``/workspace/<id>/go`` page text that includes a discovered email."""
    return (
        f"{email} "
        "rollingUsage:{usagePercent:50,resetInSec:3600} "
        "weeklyUsage:{usagePercent:30,resetInSec:86400} "
        "monthlyUsage:{usagePercent:20,resetInSec:2592000}"
    )


class TestWorkspaceDiscoveryPinsAccountId:
    """``_get_workspace_id`` is the first email-discovery site."""

    @pytest.mark.asyncio
    async def test_pins_account_id_from_email(self, mock_http_client):
        """Fresh collector (account_id=None) gets the email pinned after
        the workspace HTML is scraped."""
        collector = OpenCodeCollector()
        assert collector.account_id is None

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=_workspace_response("alice@example.com"),
            ),
        ):
            workspace_id = await collector._get_workspace_id(mock_http_client, {})

        assert workspace_id == "wrk_test"
        assert collector.account_label == "alice@example.com"
        assert collector.account_id == "alice@example.com"

    @pytest.mark.asyncio
    async def test_pins_account_id_from_default_state(self, mock_http_client):
        """``account_id="default"`` (the bug-report state) is upgraded to
        the discovered email. Covers the ``or self.account_id == "default"``
        half of the guard — the original test never exercised this path."""
        collector = OpenCodeCollector(account_id="default")
        assert collector.account_id == "default"

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=_workspace_response("bob@example.com"),
            ),
        ):
            await collector._get_workspace_id(mock_http_client, {})

        assert collector.account_id == "bob@example.com"

    @pytest.mark.asyncio
    async def test_explicit_account_id_is_preserved(self, mock_http_client):
        """An explicit ``account_id`` set at construction is never overwritten
        by the email-discovery guard — avoids aliasing two distinct identities
        when a user has multiple opencode accounts cached."""
        collector = OpenCodeCollector(account_id="alice@example.com")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=_workspace_response("bob@example.com"),
            ),
        ):
            await collector._get_workspace_id(mock_http_client, {})

        # account_label still picks up the discovered email (that's the
        # pre-fix behaviour — left alone here).
        assert collector.account_label == "bob@example.com"
        # but account_id is preserved.
        assert collector.account_id == "alice@example.com"

    @pytest.mark.asyncio
    async def test_no_email_no_account_id_change(self, mock_http_client):
        """When the workspace HTML carries no email, ``account_id`` stays
        unchanged — the guard is a no-op, not a regression."""
        collector = OpenCodeCollector(account_id="default")
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.text = 'id:"wrk_no_email"; no email here'

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "app.services.collectors.opencode.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ),
        ):
            await collector._get_workspace_id(mock_http_client, {})

        # identity is still the default — no email to stamp from.
        assert collector.account_label is None
        assert collector.account_id == "default"


class TestParseUsageDataPinsAccountId:
    """``_parse_usage_data`` is the second email-discovery site (sister
    site to the workspace-discovery path)."""

    def test_pins_account_id_from_email(self):
        """Fresh collector gets the email pinned after the subscription
        page is parsed."""
        collector = OpenCodeCollector()
        assert collector.account_id is None

        cards = collector._parse_usage_data(_subscription_text("alice@example.com"), "wrk_TEST")

        # Verify the parsing produced the expected cards and pinned the identity.
        assert cards  # at least one card emitted
        assert collector.account_label == "alice@example.com"
        assert collector.account_id == "alice@example.com"

    def test_pins_account_id_from_default_state(self):
        """``account_id="default"`` gets upgraded to the discovered email."""
        collector = OpenCodeCollector(account_id="default")

        collector._parse_usage_data(_subscription_text("bob@example.com"), "wrk_TEST")

        assert collector.account_id == "bob@example.com"

    def test_explicit_account_id_is_preserved(self):
        """An explicit ``account_id`` set at construction is never overwritten."""
        collector = OpenCodeCollector(account_id="alice@example.com")

        collector._parse_usage_data(_subscription_text("bob@example.com"), "wrk_TEST")

        assert collector.account_label == "bob@example.com"
        assert collector.account_id == "alice@example.com"
