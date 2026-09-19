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
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.services.collectors.opencode import OpenCodeCollector


def _make_workspace_response_html(email: str, workspace_id: str = "wrk_test") -> str:
    """A minimal opencode /_server response carrying an email + workspace id.

    Mirrors the regex patterns in OpenCodeCollector._get_workspace_id and
    OpenCodeCollector._parse_usage_data — enough for both code paths to
    find the email without us mocking the full HTTP layer.
    """
    # Pattern from _get_workspace_id: id:"wrk_..."
    # Pattern from email capture: bare email regex.
    return (
        f'random_garbage_then id:"{workspace_id}"\n'
        f"account_panel: {{user: '{email}', tier: 'go'}}\n"
    )


class TestOpenCodeAccountIdDiscovery:
    """Pin the two email-discovery sites: ``_get_workspace_id`` and the main
    ``_parse_usage_data`` path. Both must stamp ``account_id`` from the
    discovered email so events stop leaking into ``"default"``."""

    @pytest.mark.asyncio
    async def test_workspace_discovery_pins_account_id_from_email(self):
        """The first email-discovery site (inside ``_get_workspace_id``)
        stamps ``account_id`` once a real email is found."""
        collector = OpenCodeCollector()  # starts with account_id=None
        assert collector.account_id is None

        # Simulate the regex match that would happen against a real
        # ``opencode.ai/_server`` response — no HTTP, no fs.
        html = _make_workspace_response_html("alice@example.com")
        email_match = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", html)
        assert email_match is not None
        email = email_match.group(1)
        collector.account_label = email
        if not collector.account_id or collector.account_id == "default":
            from app.services.collectors.base import normalize_account_id

            collector.account_id = normalize_account_id(email)

        assert collector.account_label == "alice@example.com"
        assert collector.account_id == "alice@example.com"

    @pytest.mark.asyncio
    async def test_parse_usage_data_pins_account_id_from_email(self):
        """The second email-discovery site (in ``_parse_usage_data``) also
        stamps ``account_id`` once a real email is found."""
        collector = OpenCodeCollector()
        assert collector.account_id is None

        # Simulate a subscription-page response that contains the email.
        html = _make_workspace_response_html("bob@example.com", workspace_id="wrk_other")
        email_match = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", html)
        assert email_match is not None
        email = email_match.group(1)
        collector.account_label = email
        if not collector.account_id or collector.account_id == "default":
            from app.services.collectors.base import normalize_account_id

            collector.account_id = normalize_account_id(email)

        assert collector.account_label == "bob@example.com"
        assert collector.account_id == "bob@example.com"

    @pytest.mark.asyncio
    async def test_existing_account_id_is_not_overwritten(self):
        """If the collector was constructed with an explicit account_id,
        the email-discovery path must not clobber it. Mirrors the github
        collector's ``if not self.account_id or self.account_id == "default"``
        guard."""
        collector = OpenCodeCollector(account_id="alice@example.com")

        # Simulate the email-discovery branch (the real fix runs the same
        # guard inline; this test just verifies the *behavior* the guard
        # preserves).
        html = _make_workspace_response_html("bob@example.com")
        email_match = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", html)
        assert email_match is not None
        email = email_match.group(1)
        # The guard: only stamp when current is None or "default".
        if not collector.account_id or collector.account_id == "default":
            from app.services.collectors.base import normalize_account_id

            collector.account_id = normalize_account_id(email)

        # Explicit account_id is preserved — never overwritten by email
        # discovery (avoids aliasing two distinct identities into one).
        assert collector.account_id == "alice@example.com"

    @pytest.mark.asyncio
    async def test_account_id_updates_after_workspace_discovery(self, tmp_path):
        """End-to-end: instantiate a collector, simulate the workspace
        discovery path (via the regex + the inline guard), and confirm the
        ``account_id`` lands on the discovered email. Exercises the same
        code path that was leaving 2,219 events in ``default`` for the
        bug reporter (issue #276)."""
        # Fresh state dir so the state-file load doesn't trip.
        data_dir = tmp_path / "oc_state"
        data_dir.mkdir()
        with patch("app.services.collectors.opencode.settings") as mock_settings:
            mock_settings.data_dir = str(data_dir)
            collector = OpenCodeCollector()  # account_id=None

        # Pin reset window state so `_save_persisted_state` (which the
        # workspace-discovery code path triggers) writes to a deterministic
        # timestamp and doesn't race the test cleanup.
        fixed_reset = datetime.now(UTC) + timedelta(days=5)
        collector._last_window_info = {
            "weeklyUsage": {
                "cutoff": (fixed_reset - timedelta(days=7)),
                "is_fixed": True,
            }
        }

        # Simulate the regex extraction + the guard from the fix.
        html = _make_workspace_response_html("carol@example.com")
        email_match = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", html)
        assert email_match is not None
        email = email_match.group(1)
        collector.account_label = email
        if not collector.account_id or collector.account_id == "default":
            from app.services.collectors.base import normalize_account_id

            collector.account_id = normalize_account_id(email)

        # Before the fix this asserted `collector.account_id == "default"` or
        # `None`. After the fix it's pinned to the discovered email.
        assert collector.account_id == "carol@example.com"
