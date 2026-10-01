"""Pin what Token Health emits for a realistic mixed world (see ``token_health_world``).

This is a characterization test: it records current behaviour, including behaviour that
is arguably wrong, so that refactoring how status is computed changes only what a diff of
this file shows. Each tuple is (provider, account_id, source_id, status, flags...).
"""

from __future__ import annotations

import pytest

from app.services.token_health import TokenHealthService
from tests.unit.token_health_world import build_world


def _shape(rows):
    return sorted(
        (
            r["provider"],
            r["account_id"],
            r.get("source_id"),
            r["status"],
            bool(r["redundant"]),
            bool(r["removable"]),
            bool(r.get("assignment_pending")),
            bool(r.get("identity_pending")),
            tuple(r["token_types"]),
        )
        for r in rows
    )


@pytest.mark.asyncio
async def test_world_rows(monkeypatch):
    await build_world(monkeypatch)
    rows = await TokenHealthService().get_health()

    assert _shape(rows) == sorted(
        [
            # Pending Claude bundle that exists only in the cache.
            (
                "anthropic",
                "unassigned:sidecar:pending",
                "sidecar:pending",
                "valid",
                False,
                False,
                True,
                False,
                ("oauth_token", "refresh_token"),
            ),
            # Aggregate-only entry (no source id), expired but carrying a refresh token.
            (
                "chatgpt",
                "alice@example.com",
                None,
                "expired",
                False,
                True,
                False,
                False,
                ("oauth_token", "refresh_token", "expiry_date"),
            ),
            # Same account on two machines: one row each. Both carry a refresh token and
            # expire in 2h, so the server rolls them before they lapse: "valid". (They used
            # to read "expiring" — durable rows weren't marked rollable, so the auto-refresh
            # allowance in the classification never applied.)
            (
                "gemini",
                "alice@example.com",
                "sidecar:g1",
                "valid",
                False,
                False,
                False,
                False,
                ("oauth_token", "refresh_token", "expiry_date"),
            ),
            (
                "gemini",
                "alice@example.com",
                "sidecar:g2",
                "valid",
                False,
                False,
                False,
                False,
                ("oauth_token", "refresh_token", "expiry_date"),
            ),
            # A machine that stopped reporting.
            (
                "gemini",
                "alice@example.com",
                "sidecar:g3",
                "stale",
                False,
                False,
                False,
                False,
                ("oauth_token",),
            ),
            # Server env credential: a synthetic "server" account.
            ("github", "server", None, "valid", False, False, False, False, ("api_key",)),
            # Pasted cookie / key: synthetic config accounts.
            (
                "ollama",
                "config-cookie:default",
                None,
                "valid",
                False,
                False,
                False,
                False,
                ("session_cookie",),
            ),
            (
                "openrouter",
                "config:default",
                None,
                "valid",
                False,
                False,
                False,
                False,
                ("api_key",),
            ),
            # Rejected by the provider (flagged in auth_failures).
            (
                "openrouter",
                "bob@example.com",
                "sidecar:o1",
                "invalid",
                False,
                False,
                False,
                False,
                ("api_key",),
            ),
        ]
    )


@pytest.mark.asyncio
async def test_world_row_origins(monkeypatch):
    """Origin columns the UI and alerts rely on: machine name for sidecar rows,
    ``config``/``server`` for rows the server itself holds."""
    await build_world(monkeypatch)
    rows = {
        (r["provider"], r["account_id"], r.get("source_id")): r
        for r in await TokenHealthService().get_health()
    }
    assert rows[("gemini", "alice@example.com", "sidecar:g1")]["source_name"] == "DEV-01"
    assert rows[("gemini", "alice@example.com", "sidecar:g2")]["source_name"] == "MacBook"
    assert rows[("gemini", "alice@example.com", "sidecar:g3")]["source_name"] == "Gone"
    assert rows[("github", "server", None)]["source_name"] == "server"
    assert rows[("openrouter", "config:default", None)]["source_name"] == "config"
    assert rows[("chatgpt", "alice@example.com", None)]["source_name"] == "MacBook"
