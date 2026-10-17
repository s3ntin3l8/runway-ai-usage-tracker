"""Claude Desktop cookies use their own Keychain key and Chromium host binding."""

import hashlib
import sqlite3
import subprocess
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from scripts import sidecar


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sidecar.Path, "home", lambda: tmp_path)
    path = tmp_path / "Library/Application Support/Claude/Cookies"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('version', '24')")
        conn.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, expires_utc INTEGER)"
        )
    return path


def write_cookie(path, *, host=".claude.ai", digest_host=None, expired=False, corrupt=False):
    key = hashlib.pbkdf2_hmac("sha1", b"synthetic-storage-key", b"saltysalt", 1003, 16)
    data = hashlib.sha256((digest_host or host).encode()).digest() + b"sk-ant-synthetic"
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    encrypted = b"v10" + encryptor.update(padded) + encryptor.finalize()
    if corrupt:
        encrypted = b"v10invalid"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?)",
            (host, "sessionKey", encrypted, 1 if expired else 0),
        )


def storage_key(monkeypatch):
    lookup = Mock(return_value=subprocess.CompletedProcess([], 0, "synthetic-storage-key\n", ""))
    monkeypatch.setattr(sidecar.subprocess, "run", lookup)
    return lookup


def test_desktop_cookie_is_discovered_and_host_prefix_removed(desktop, monkeypatch):
    write_cookie(desktop)
    storage_key(monkeypatch)
    assert (
        sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey") == "sk-ant-synthetic"
    )


def test_unavailable_keychain_is_backed_off_and_logs_no_secret(desktop, monkeypatch, caplog):
    write_cookie(desktop)
    lookup = Mock(return_value=subprocess.CompletedProcess([], 44, "", "sensitive stderr"))
    monkeypatch.setattr(sidecar.subprocess, "run", lookup)
    for _ in range(2):
        assert sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey") is None
    assert lookup.call_count == 1
    assert "Claude Desktop" in caplog.text
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
    assert sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey") is None


def test_desktop_is_not_scanned_for_unrelated_provider(desktop, monkeypatch):
    write_cookie(desktop)
    lookup = storage_key(monkeypatch)
    assert sidecar.BrowserCookieExtractor.get_cookie("example.com", "sessionKey") is None
    assert lookup.call_count == 0


def test_missing_desktop_store_does_not_access_keychain(desktop, monkeypatch):
    desktop.unlink()
    lookup = storage_key(monkeypatch)
    assert sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey") is None
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
    with sqlite3.connect(desktop) as conn:
        conn.execute("UPDATE cookies SET encrypted_value = NULL")
    lookup = storage_key(monkeypatch)
    assert sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey") is None
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
    assert (
        sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey") == "sk-ant-synthetic"
    )
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(
            "security", 5, output="sensitive output", stderr="sensitive error"
        ),
        FileNotFoundError("sensitive path"),
    ],
)
def test_keychain_command_failure_is_reported_and_backed_off(desktop, monkeypatch, caplog, failure):
    from scripts.sidecar_pkg import claude_desktop

    monkeypatch.setattr(claude_desktop, "_WARNED", set())
    write_cookie(desktop)
    lookup = Mock(side_effect=failure)
    monkeypatch.setattr(sidecar.subprocess, "run", lookup)
    for _ in range(2):
        assert sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey") is None
    assert lookup.call_count == 1
    assert "Keychain command did not complete" in caplog.text
    assert "sensitive" not in caplog.text


@pytest.mark.parametrize("missing", ["helper", "padding", "ciphers"])
def test_missing_desktop_dependency_preserves_browser_fallback(desktop, monkeypatch, missing):
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
            raise ImportError("synthetic missing dependency")
        return original_import(name, global_ns, local_ns, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert (
        sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey")
        == "synthetic-browser-cookie"
    )


def test_desktop_cookie_source_is_logged_without_value(desktop, monkeypatch, caplog):
    caplog.set_level("INFO")
    write_cookie(desktop)
    storage_key(monkeypatch)
    assert (
        sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey") == "sk-ant-synthetic"
    )
    assert "source: Claude Desktop" in caplog.text
    assert "sk-ant-synthetic" not in caplog.text


def test_desktop_cookie_precedes_readable_browser_cookie(desktop, monkeypatch):
    write_cookie(desktop)
    storage_key(monkeypatch)
    browser_paths = Mock(return_value=[{"type": "safari", "path": desktop}])
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", browser_paths)
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "parse_safari",
        lambda path: [{"domain": "claude.ai", "name": "sessionKey", "value": "browser-cookie"}],
    )
    assert (
        sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey") == "sk-ant-synthetic"
    )
    browser_paths.assert_not_called()
