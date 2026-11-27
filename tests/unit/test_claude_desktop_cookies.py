"""Claude Desktop cookies use their own Keychain key and Chromium host binding.

On macOS the sidecar reads Claude cookies through ``get_macos_candidates``, so these
tests go through it; ``get_cookie`` serves the other platforms and never reads Desktop.
"""

import hashlib
import sqlite3
import subprocess
from contextlib import closing
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from scripts import sidecar
from scripts.sidecar_pkg import macos_keychain_store as keychain
from scripts.sidecar_pkg.macos_keychain_store import KeychainError
from tests.macos_cookie_fixtures import approved_keychain_readers  # noqa: F401


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    from scripts.sidecar_pkg import macos_cookies

    monkeypatch.setattr(macos_cookies, "_KEY_CACHE", {})
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sidecar.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [])
    path = tmp_path / "Library/Application Support/Claude/Cookies"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('version', '24')")
        conn.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, expires_utc INTEGER)"
        )
    return path


def write_cookie(
    path,
    *,
    host=".claude.ai",
    digest_host=None,
    expired=False,
    corrupt=False,
    value=b"sk-ant-synthetic",
):
    key = hashlib.pbkdf2_hmac("sha1", b"synthetic-storage-key", b"saltysalt", 1003, 16)
    data = hashlib.sha256((digest_host or host).encode()).digest() + value
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    encrypted = b"v10" + encryptor.update(padded) + encryptor.finalize()
    if corrupt:
        encrypted = b"v10invalid"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?)",
            (host, "sessionKey", encrypted, 1 if expired else 0),
        )


def claude_values(provider="anthropic", domains=("claude.ai",)):
    """The cookie values the macOS lookup returns, in the order it returns them."""
    return [
        value
        for value, _origin in sidecar.BrowserCookieExtractor.get_macos_candidates(
            provider, list(domains), "sessionKey"
        )
    ]


def storage_key(monkeypatch):
    lookup = Mock(return_value=(b"synthetic-storage-key\n"))
    monkeypatch.setattr(keychain, "read_secret", lookup)
    return lookup


def test_desktop_cookie_is_discovered_and_host_prefix_removed(desktop, monkeypatch):
    write_cookie(desktop)
    storage_key(monkeypatch)
    assert claude_values() == ["sk-ant-synthetic"]


def test_unavailable_keychain_is_backed_off_and_logs_no_secret(desktop, monkeypatch, caplog):
    write_cookie(desktop)
    lookup = Mock(side_effect=KeychainError("missing"))
    monkeypatch.setattr(keychain, "read_secret", lookup)
    for _ in range(2):
        assert claude_values() == []
    assert lookup.call_count == 1
    assert "Keychain access unavailable" in caplog.text
    assert "sensitive stderr" not in caplog.text


@pytest.mark.parametrize(
    "options",
    [
        {"digest_host": ".other.example"},
        {"host": "evil-claude.ai"},
        {"expired": True},
        {"corrupt": True},
    ],
)
def test_invalid_or_expired_cookie_cannot_supply_credentials(desktop, monkeypatch, options):
    write_cookie(desktop, **options)
    storage_key(monkeypatch)
    assert claude_values() == []


def test_desktop_is_not_scanned_for_unrelated_provider(desktop, monkeypatch):
    write_cookie(desktop)
    lookup = storage_key(monkeypatch)
    assert claude_values("example", ["example.com"]) == []
    assert lookup.call_count == 0


def test_missing_desktop_store_does_not_access_keychain(desktop, monkeypatch):
    desktop.unlink()
    lookup = storage_key(monkeypatch)
    assert claude_values() == []
    assert lookup.call_count == 0


@pytest.mark.parametrize("sample_time", [0, 1792187827777])
def test_quota_history_cannot_become_fresh_token_usage(desktop, monkeypatch, sample_time):
    """Old or recent local percentages must not be presented as token counts."""
    import json

    (desktop.parent / "plan-usage-history.json").write_text(
        json.dumps(
            {
                "version": 2,
                "samples": [{"t": sample_time, "org": "synthetic-org", "u": {"fh": 70, "sd": 100}}],
            }
        )
    )
    write_cookie(desktop)
    storage_key(monkeypatch)
    cards, blocked = sidecar.GenericCollector.collect_provider(
        "anthropic",
        {
            "name": "Claude Pro",
            "icon": "test",
            "rules": [
                {
                    "type": "cookie",
                    "domains": ["claude.ai"],
                    "name": "sessionKey",
                    "mapping": {"value": "cookie_sessionKey"},
                }
            ],
        },
    )
    assert len(cards) == 1
    assert cards[0]["remaining"] == "Token"
    assert cards[0]["unit"] == "cookie"
    assert cards[0]["account_id"] is None
    assert cards[0]["metadata"]["identity_pending"] is True
    assert cards[0]["metadata"]["cookie_sessionKey"] == "sk-ant-synthetic"
    assert "pct_used" not in cards[0]
    assert "token_usage" not in cards[0]
    assert blocked


def test_malformed_cookie_blob_does_not_abort_provider_collection(desktop, monkeypatch):
    write_cookie(desktop)
    with closing(sqlite3.connect(desktop)) as conn, conn:
        conn.execute("UPDATE cookies SET encrypted_value = NULL")
    lookup = storage_key(monkeypatch)
    assert claude_values() == []
    assert lookup.call_count == 0


def test_desktop_cookie_database_is_closed_after_read(desktop, monkeypatch):
    write_cookie(desktop)
    storage_key(monkeypatch)
    connect = sqlite3.connect
    connections = []

    def track_connection(*args, **kwargs):
        conn = connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", track_connection)
    assert claude_values() == ["sk-ant-synthetic"]
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize(
    "failure",
    [
        KeychainError("timed_out"),
        FileNotFoundError("sensitive path"),
    ],
)
def test_keychain_command_failure_is_reported_and_backed_off(desktop, monkeypatch, caplog, failure):
    from scripts.sidecar_pkg import claude_desktop

    monkeypatch.setattr(claude_desktop, "_WARNED", set())
    write_cookie(desktop)
    lookup = Mock(side_effect=failure)
    monkeypatch.setattr(keychain, "read_secret", lookup)
    for _ in range(2):
        assert claude_values() == []
    assert lookup.call_count == 1
    assert "Keychain access unavailable" in caplog.text
    assert "sensitive" not in caplog.text


@pytest.mark.parametrize("missing", ["helper", "padding", "ciphers"])
def test_missing_desktop_dependency_preserves_browser_fallback(
    desktop, monkeypatch, caplog, missing
):
    import builtins

    write_cookie(desktop)
    storage_key(monkeypatch)
    browser_db = desktop.parent / "synthetic-browser.sqlite"
    with sqlite3.connect(browser_db) as conn:
        conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO moz_cookies VALUES ('.claude.ai', 'sessionKey', 'synthetic-browser-cookie')"
        )
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "get_all_paths",
        lambda: [
            {"browser": "Firefox", "type": "firefox", "path": browser_db},
        ],
    )
    original_import = builtins.__import__

    def guarded_import(name, global_ns=None, local_ns=None, fromlist=(), level=0):
        if (
            (missing == "helper" and name == "scripts.sidecar_pkg.claude_desktop")
            or (
                missing == "padding"
                and name == "cryptography.hazmat.primitives"
                and "padding" in fromlist
            )
            or (missing == "ciphers" and name == "cryptography.hazmat.primitives.ciphers")
        ):
            raise ImportError("synthetic missing dependency", name=name)
        return original_import(name, global_ns, local_ns, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert claude_values() == ["synthetic-browser-cookie"]
    if missing == "helper":
        assert "cannot import scripts.sidecar_pkg.claude_desktop" in caplog.text


def test_cookie_sources_are_logged_without_value(desktop, monkeypatch, caplog):
    caplog.set_level("INFO")
    write_cookie(desktop)
    storage_key(monkeypatch)
    store = firefox_store(desktop.parent)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [store])
    candidates = sidecar.BrowserCookieExtractor.get_macos_candidates(
        "anthropic", ["claude.ai"], "sessionKey"
    )
    assert [value for value, _ in candidates] == ["sk-ant-synthetic", "synthetic-browser"]
    assert "macOS cookie scan summary: scans=1" in caplog.text
    for source, (_value, origin) in zip(["Claude Desktop", "Firefox"], candidates, strict=True):
        fingerprint = origin.partition("#")[2]
        assert fingerprint
        assert f"[anthropic] cookie source: {source} #{fingerprint}" in caplog.text
    assert "sk-ant-synthetic" not in caplog.text
    assert "synthetic-browser" not in caplog.text
    assert "synthetic.default" not in caplog.text


def firefox_store(directory, value="synthetic-browser"):
    browser = directory / "firefox.sqlite"
    with closing(sqlite3.connect(browser)) as conn, conn:
        conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
        conn.execute("INSERT INTO moz_cookies VALUES ('.claude.ai', 'sessionKey', ?)", (value,))
    return {
        "browser": "Firefox",
        "type": "firefox",
        "path": browser,
        "profile": "synthetic.default",
        "store": "cookies.sqlite",
    }


def test_desktop_cookie_comes_first_and_browser_cookie_is_still_returned(desktop, monkeypatch):
    write_cookie(desktop)
    storage_key(monkeypatch)
    store = firefox_store(desktop.parent)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [store])
    assert claude_values() == ["sk-ant-synthetic", "synthetic-browser"]


CLAUDE_COOKIE_RULES = {
    "name": "Claude Pro",
    "icon": "test",
    "rules": [
        {
            "type": "cookie",
            "domains": ["claude.ai"],
            "name": "sessionKey",
            "mapping": {"value": "cookie_sessionKey"},
        }
    ],
}


def collected_sources(account_label_hints=None):
    """Cookie value, origin and account of each card the sidecar's collection sends."""
    cards, _blocked = sidecar.GenericCollector.collect_provider(
        "anthropic", CLAUDE_COOKIE_RULES, account_label_hints=account_label_hints
    )
    return [
        (
            card["metadata"]["cookie_sessionKey"],
            card["metadata"]["credential_origin"],
            card["account_id"],
        )
        for card in cards
    ]


def test_collection_sends_desktop_and_browser_cookies_as_separate_sources(desktop, monkeypatch):
    """A Desktop cookie the server rejects cannot hide the browser one (#1406).

    Both arrive as their own fingerprinted source, so the server's per-source failover
    moves to the browser cookie once the Desktop one is marked auth_failed.
    """
    write_cookie(desktop)
    storage_key(monkeypatch)
    store = firefox_store(desktop.parent)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [store])
    sources = collected_sources()
    assert [value for value, _, _ in sources] == ["sk-ant-synthetic", "synthetic-browser"]
    desktop_origin, browser_origin = (origin for _, origin, _ in sources)
    assert desktop_origin.startswith("cookie:anthropic/Claude Desktop/Default/Cookies#")
    assert browser_origin.startswith("cookie:anthropic/Firefox/synthetic.default/cookies.sqlite#")
    assert all(value not in origin for value, origin, _ in sources)


def test_browser_cookie_of_another_account_keeps_its_own_identity(desktop, monkeypatch):
    write_cookie(desktop)
    storage_key(monkeypatch)
    store = firefox_store(desktop.parent)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [store])
    (_, desktop_origin, _), (_, browser_origin, _) = collected_sources()
    sources = collected_sources(
        {
            "anthropic": {
                desktop_origin: "desktop@example.com",
                browser_origin: "browser@example.com",
                # Neither the store-level Desktop origin nor the provider fallback may
                # carry the Desktop account over to the browser cookie.
                "cookie:anthropic/Claude Desktop/Default/Cookies": "desktop@example.com",
                "provider:anthropic": "desktop@example.com",
            }
        }
    )
    assert [(value, account) for value, _, account in sources] == [
        ("sk-ant-synthetic", "desktop@example.com"),
        ("synthetic-browser", "browser@example.com"),
    ]
    # Before the server has verified the browser cookie, it stays unattributed.
    untagged = collected_sources(
        {
            "anthropic": {
                desktop_origin: "desktop@example.com",
                "cookie:anthropic/Claude Desktop/Default/Cookies": "desktop@example.com",
                "provider:anthropic": "desktop@example.com",
            }
        }
    )
    assert [(value, account) for value, _, account in untagged] == [
        ("sk-ant-synthetic", "desktop@example.com"),
        ("synthetic-browser", None),
    ]


def test_new_desktop_login_gets_a_new_origin_and_stays_first(desktop, monkeypatch):
    write_cookie(desktop)
    storage_key(monkeypatch)
    store = firefox_store(desktop.parent)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [store])
    (_, rejected_origin, _), (_, browser_origin, _) = collected_sources()
    with closing(sqlite3.connect(desktop)) as conn, conn:
        conn.execute("DELETE FROM cookies")
    write_cookie(desktop, value=b"sk-ant-relogin")
    sources = collected_sources()
    assert [value for value, _, _ in sources] == ["sk-ant-relogin", "synthetic-browser"]
    assert sources[0][1].startswith("cookie:anthropic/Claude Desktop/Default/Cookies#")
    assert sources[0][1] != rejected_origin
    assert sources[1][1] == browser_origin


def test_desktop_opt_out_preserves_browser_without_keychain(desktop, monkeypatch):
    from scripts.sidecar_pkg import claude_desktop

    monkeypatch.setenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "off")

    def forbidden(*args, **kwargs):
        raise AssertionError("Desktop credential access must be skipped")

    monkeypatch.setattr(claude_desktop, "scan_session_cookie", forbidden)
    store = firefox_store(desktop.parent)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [store])
    assert claude_values() == ["synthetic-browser"]


@pytest.fixture(autouse=True)
def isolate_host_credentials(monkeypatch, tmp_path):
    """Synthetic tests must never inspect host browser stores or Keychain."""
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original_run = subprocess.run

    def guarded_run(command, *args, **kwargs):
        if (
            isinstance(command, (list, tuple))
            and command
            and Path(str(command[0])).name == "security"
        ):
            raise AssertionError("Live Keychain access is forbidden in unit tests")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
