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


async def test_opencode_api_key_never_crosses_accounts(monkeypatch):
    """Two accounts side by side in the token cache keep their own CLI key.

    #347's account-isolation half on the read path: the OpenCode credential
    is a bare API key, so inheriting another account's entry would silently
    report that account's quota under this one's card."""
    cache = TokenCache()
    await cache.store(
        "opencode",
        {"api_key": "oc_alices_key"},  # pragma: allowlist secret
        account_id="alice@example.com",
    )
    await cache.store(
        "opencode",
        {"api_key": "oc_bobs_key"},  # pragma: allowlist secret
        account_id="bob@example.com",
    )
    monkeypatch.setattr("app.services.collectors.opencode.token_cache", cache)

    alice_tokens, _ = await OpenCodeCollector(account_id="alice@example.com")._get_credentials()
    bob_tokens, _ = await OpenCodeCollector(account_id="bob@example.com")._get_credentials()

    assert alice_tokens["api_key"] == "oc_alices_key"  # pragma: allowlist secret
    assert bob_tokens["api_key"] == "oc_bobs_key"  # pragma: allowlist secret


async def test_opencode_identity_stamped_from_the_credential_owner_label(monkeypatch):
    """The label the sidecar attached to the credential is the collector's
    identity, not whatever the API response scrapes (#315, kept green by the
    #347 identity tagging): a fresh collector reads its credential, then pins
    itself to the credential's owner."""
    cache = TokenCache()
    await cache.store(
        "opencode",
        {"api_key": "oc_alices_key"},  # pragma: allowlist secret
        account_id="default",
        account_label="work@example.com",
    )
    monkeypatch.setattr("app.services.collectors.opencode.token_cache", cache)
    collector = OpenCodeCollector()  # fresh — no identity pinned yet

    tokens, _ = await collector._get_credentials()
    assert tokens["api_key"] == "oc_alices_key"  # pragma: allowlist secret
    assert collector._cookie_owner == "work@example.com"

    collector._pin_identity("scraped-personal@example.com")

    assert collector.account_id == "work@example.com"
    assert collector.account_label == "work@example.com"


def test_opencode_falls_back_to_workspace_scrape_without_cookie_identity():
    collector = OpenCodeCollector()
    collector._pin_identity("Personal@Example.com")
    assert collector.account_id == "personal@example.com"


def test_unpinned_collector_bootstraps_from_the_only_identified_account():
    """Fresh single-account server: the unpinned collector may borrow the one
    identified entry — that first card is what pins it (#327 review)."""
    from app.services.token_cache import borrowable_entries

    entries = [{"account_id": "alice@example.com", "tokens": {"oauth_token": "t"}}]
    assert borrowable_entries(entries, None) == entries


def test_unpinned_collector_does_not_guess_between_two_accounts(caplog):
    from app.services.token_cache import borrowable_entries

    entries = [
        {"account_id": "alice@example.com", "tokens": {}},
        {"account_id": "bob@example.com", "tokens": {}},
    ]
    with caplog.at_level("WARNING"):
        assert borrowable_entries(entries, None, provider="chatgpt") == []
    assert "belong to other accounts" in caplog.text


async def test_chatgpt_fallback_bootstraps_single_account(monkeypatch):
    from app.services.collectors import chatgpt_oauth
    from app.services.collectors.chatgpt import ChatGPTCollector

    cache = TokenCache()
    await cache.store("chatgpt", {"oauth_token": "alices-token"}, account_id="alice@example.com")
    monkeypatch.setattr(chatgpt_oauth, "token_cache", cache)

    found = await ChatGPTCollector(account_id=None)._find_cross_account_oauth_token()
    assert found is not None
    assert found[0]["oauth_token"] == "alices-token"


async def test_same_account_cli_and_browser_credentials_coexist():
    cache = TokenCache()
    await cache.store("chatgpt", {"oauth_token": "cli-token"}, account_id="a@example.com")
    await cache.store("chatgpt", {"cookie_session": "browser-cookie"}, account_id="a@example.com")
    stored = await cache.get("chatgpt", "a@example.com")
    assert stored == {"oauth_token": "cli-token", "cookie_session": "browser-cookie"}


async def test_different_account_cli_and_browser_credentials_stay_scoped():
    cache = TokenCache()
    await cache.store("chatgpt", {"oauth_token": "cli-a"}, account_id="a@example.com")
    await cache.store("chatgpt", {"cookie_session": "browser-b"}, account_id="b@example.com")
    assert await cache.get("chatgpt", "a@example.com") == {"oauth_token": "cli-a"}
    assert await cache.get("chatgpt", "b@example.com") == {"cookie_session": "browser-b"}


async def test_same_account_stale_oauth_push_keeps_fresh_oauth_and_browser_cookie():
    cache = TokenCache()
    await cache.store(
        "chatgpt",
        {
            "oauth_token": "fresh",
            "expiry_date": "9999999999999",
            "cookie_session": "fresh-cookie",
        },
        account_id="a@example.com",
    )
    await cache.store(
        "chatgpt",
        {
            "oauth_token": "expired",
            "expiry_date": "1",
            "cookie_session": "stale-cookie",
        },
        account_id="a@example.com",
    )
    stored = await cache.get("chatgpt", "a@example.com")
    assert stored["oauth_token"] == "fresh"
    assert stored["cookie_session"] == "fresh-cookie"
