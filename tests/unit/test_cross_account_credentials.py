"""Collectors must never report one account's quota with another's credential."""

from __future__ import annotations

import pytest

from app.services.collectors.opencode import OpenCodeCollector
from app.services.token_cache import TokenCache, is_foreign_account_entry


@pytest.mark.parametrize(
    ("entry", "wanted", "foreign"),
    [
        ("default", "alice@example.com", False),  # identity-less: may borrow
        ("opaque-hash-id", "alice@example.com", False),  # opaque id: may borrow
        ("alice@example.com", "alice@example.com", False),  # itself
        ("Alice@Example.com", "alice@example.com", False),  # itself, other case
        ("bob@example.com", "alice@example.com", True),  # another account
        ("bob@example.com", None, True),
    ],
)
def test_is_foreign_account_entry(entry, wanted, foreign):
    assert is_foreign_account_entry(entry, wanted) is foreign


async def test_antigravity_fallback_skips_other_accounts_token(monkeypatch):
    from app.services.collectors import antigravity_oauth

    cache = TokenCache()
    await cache.store(
        "antigravity",
        {"oauth_token": "bobs-token", "expiry_date": "9999999999999"},
        account_id="bob@example.com",
    )
    monkeypatch.setattr(antigravity_oauth, "token_cache", cache)

    from app.services.collectors.antigravity import AntigravityCollector

    collector = AntigravityCollector(account_id="alice@example.com")
    token = await collector._get_current_token()

    assert token is None or "bobs-token" not in str(token)


def test_opencode_pins_to_cookie_owner_over_workspace_scrape():
    collector = OpenCodeCollector()
    collector._cookie_owner = "work@example.com"
    collector._pin_identity("personal@example.com")
    assert collector.account_id == "work@example.com"
    assert collector.account_label == "work@example.com"


def test_opencode_falls_back_to_workspace_scrape_without_cookie_identity():
    collector = OpenCodeCollector()
    collector._pin_identity("Personal@Example.com")
    assert collector.account_id == "personal@example.com"
