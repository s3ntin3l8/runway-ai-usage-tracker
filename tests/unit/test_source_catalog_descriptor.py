"""How the settings pages group Keychain sources: by browser, by service and by Keychain item."""

import pytest

from scripts.sidecar_pkg.source_catalog import BROWSERS, catalog


@pytest.mark.parametrize(
    "source_id, browser, service",
    [
        ("anthropic.chrome", "chrome", "Claude Pro"),
        ("anthropic.edge", "edge", "Claude Pro"),
        ("anthropic.chromium", "chromium", "Claude Pro"),
        ("chatgpt.chrome", "chrome", "ChatGPT Codex"),
        ("anthropic.claude-code", None, "Claude Code"),
        ("anthropic.desktop-session", None, "Claude Desktop session"),
        ("github.gh-cli", None, "GitHub CLI"),
    ],
)
def test_browser_and_service_come_from_the_id_and_label(source_id, browser, service):
    descriptor = catalog()[source_id]
    assert descriptor.browser_key == browser
    assert descriptor.service == service


def test_every_cookie_source_names_one_of_the_known_browsers():
    cookie = [d for d in catalog().values() if d.browser_key]
    assert cookie and {d.browser_key for d in cookie} == set(BROWSERS)
    assert all(BROWSERS[d.browser_key] in d.label for d in cookie)


def test_sources_reading_the_same_keychain_item_share_an_item_key():
    sources = catalog()
    assert sources["anthropic.chrome"].item_key == sources["kimi_coding.chrome"].item_key
    assert sources["anthropic.chrome"].item_key != sources["anthropic.edge"].item_key
    # Claude Desktop's OAuth token and its web session live in the same Safe Storage item.
    assert (
        sources["anthropic.desktop-oauth"].item_key == sources["anthropic.desktop-session"].item_key
    )
    assert sources["anthropic.claude-code"].item_key not in {
        d.item_key for i, d in sources.items() if i != "anthropic.claude-code"
    }
