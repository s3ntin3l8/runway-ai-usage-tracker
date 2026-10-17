"""Synthetic Chromium stores; no real browser or Keychain access."""

import hashlib
import json
import sqlite3
import subprocess
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from scripts import sidecar


def encrypted(host, value=b"synthetic-session", version=24):
    raw = (hashlib.sha256(host.encode()).digest() if version >= 24 else b"") + value
    pad = padding.PKCS7(128).padder()
    raw = pad.update(raw) + pad.finalize()
    key = hashlib.pbkdf2_hmac("sha1", b"synthetic-key", b"saltysalt", 1003, 16)
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + enc.update(raw) + enc.finalize()


@pytest.fixture
def helper(monkeypatch):
    from scripts.sidecar_pkg import macos_cookies

    monkeypatch.setattr(macos_cookies, "_KEY_CACHE", {})
    lookup = Mock(return_value=subprocess.CompletedProcess([], 0, "synthetic-key", ""))
    monkeypatch.setattr(subprocess, "run", lookup)
    return macos_cookies, lookup


@pytest.mark.parametrize("version", [23, 24])
def test_decrypt_removes_only_schema_bound_prefix(helper, version):
    module, lookup = helper
    assert (
        module.decrypt(
            encrypted(".claude.ai", version=version), "Chrome Safe Storage", ".claude.ai", version
        )
        == "synthetic-session"
    )
    assert (
        module.decrypt(
            encrypted(".claude.ai", version=version), "Chrome Safe Storage", ".claude.ai", version
        )
        == "synthetic-session"
    )
    assert lookup.call_count == 1


@pytest.mark.parametrize(
    "blob,host",
    [(encrypted(".claude.ai"), "claude.ai"), (b"v20xxx", ".claude.ai"), (b"v10bad", ".claude.ai")],
)
def test_invalid_binding_or_ciphertext_rejected(helper, blob, host):
    module, _ = helper
    assert module.decrypt(blob, "Chrome Safe Storage", host, 24) is None


@pytest.mark.parametrize(
    "failure",
    [subprocess.CompletedProcess([], 44, "", ""), subprocess.TimeoutExpired("security", 5)],
)
def test_keychain_failure_suppressed_until_restart(helper, monkeypatch, failure):
    module, lookup = helper
    lookup.side_effect = failure if isinstance(failure, Exception) else None
    lookup.return_value = failure
    for _ in range(3):
        assert (
            module.decrypt(encrypted(".claude.ai"), "Chrome Safe Storage", ".claude.ai", 24) is None
        )
    assert lookup.call_count == 1


def test_profile_four_and_filesystem_profile_are_discovered(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sidecar.Path, "home", lambda: tmp_path)
    base = tmp_path / "Library/Application Support/Google/Chrome"
    for profile in ["Profile 4", "Profile 12"]:
        path = base / profile / "Network/Cookies"
        path.parent.mkdir(parents=True)
        path.touch()
    (base / "Local State").write_text(
        json.dumps({"profile": {"info_cache": {"Profile 4": {}, "../escape": {}}}})
    )
    paths = sidecar.BrowserCookieExtractor.get_all_paths()
    assert {p["profile"] for p in paths} == {"Profile 4", "Profile 12"}


def test_console_and_subscription_keep_each_profile_candidate(tmp_path, monkeypatch, helper):
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sidecar.Path, "home", lambda: tmp_path)
    for number in [4, 12]:
        path = (
            tmp_path / f"Library/Application Support/Google/Chrome/Profile {number}/Network/Cookies"
        )
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
            conn.execute("INSERT INTO meta VALUES ('version', '24')")
            conn.execute(
                "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, expires_utc INTEGER)"
            )
            for host in [".claude.ai", ".platform.claude.com"]:
                conn.execute(
                    "INSERT INTO cookies VALUES (?, ?, ?, 0)",
                    (host, "sessionKey", encrypted(host, f"session-{number}-{host}".encode())),
                )
    for provider, domain, field in [
        ("anthropic", "claude.ai", "cookie_sessionKey"),
        ("anthropic_console", "platform.claude.com", "session_cookie"),
    ]:
        cards, _ = sidecar.GenericCollector.collect_provider(
            provider,
            {
                "name": "Test",
                "rules": [
                    {
                        "type": "cookie",
                        "domains": [domain],
                        "name": "sessionKey",
                        "mapping": {"value": field},
                    }
                ],
            },
        )
        assert len(cards) == 2
        assert {c["metadata"][field] for c in cards} == {
            f"session-4-.{domain}",
            f"session-12-.{domain}",
        }
        assert all(c["metadata"]["identity_pending"] for c in cards)
        assert all(f"cookie:{provider}/Chrome/Profile " in str(c["metadata"]) for c in cards)


def test_desktop_keychain_failure_does_not_retry_after_ten_minutes(tmp_path, monkeypatch):
    from scripts.sidecar_pkg import claude_desktop, macos_cookies

    monkeypatch.setattr(macos_cookies, "_KEY_CACHE", {})
    lookup = Mock(return_value=subprocess.CompletedProcess([], 44, "", ""))
    monkeypatch.setattr(subprocess, "run", lookup)
    monkeypatch.setattr(claude_desktop.time, "monotonic", lambda: 0)
    assert claude_desktop._storage_key(tmp_path) is None
    monkeypatch.setattr(claude_desktop.time, "monotonic", lambda: 10000)
    assert claude_desktop._storage_key(tmp_path) is None
    assert lookup.call_count == 1


def test_get_cookie_browser_wrapper_passes_schema_and_exact_host(tmp_path, monkeypatch, helper):
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "off")
    path = tmp_path / "Cookies"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('version', '24')")
        conn.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, expires_utc INTEGER)"
        )
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, 0)",
            (".claude.ai", "sessionKey", encrypted(".claude.ai")),
        )
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "get_all_paths",
        lambda: [{"browser": "Chrome", "type": "chromium", "path": path}],
    )
    assert (
        sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey") == "synthetic-session"
    )


@pytest.fixture(autouse=True)
def isolate_host_credentials(monkeypatch, tmp_path):
    """Synthetic tests must never inspect host browser stores or Keychain."""
    import subprocess
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


def test_v11_legacy_cookie_is_supported(helper):
    module, _ = helper
    assert (
        module.decrypt(
            b"v11" + encrypted(".claude.ai", version=23)[3:],
            "Chrome Safe Storage",
            ".claude.ai",
            23,
        )
        == "synthetic-session"
    )


def test_control_characters_are_rejected(helper):
    module, _ = helper
    assert (
        module.decrypt(
            encrypted(".claude.ai", b"invalid\nsession"), "Chrome Safe Storage", ".claude.ai", 24
        )
        is None
    )


def test_distinct_stores_keep_provenance_but_hardlinks_do_not_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    base = tmp_path / "Library/Application Support/Google/Chrome/Default"
    (base / "Network").mkdir(parents=True)
    (base / "Network/Cookies").touch()
    (base / "Cookies").touch()
    paths = sidecar.BrowserCookieExtractor.get_all_paths()
    assert {p["store"] for p in paths} == {"Cookies", "Network/Cookies"}
    (base / "Cookies").unlink()
    (base / "Cookies").hardlink_to(base / "Network/Cookies")
    assert len(sidecar.BrowserCookieExtractor.get_all_paths()) == 1


@pytest.mark.parametrize("browser_type", ["firefox", "safari"])
def test_existing_plaintext_browser_candidates_are_preserved(tmp_path, monkeypatch, browser_type):
    path = tmp_path / "Default/cookies.sqlite"
    path.parent.mkdir()
    if browser_type == "firefox":
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
            conn.execute(
                "INSERT INTO moz_cookies VALUES ('.claude.ai', 'sessionKey', 'synthetic-firefox')"
            )
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "get_all_paths",
        lambda: [{"browser": browser_type.title(), "type": browser_type, "path": path}],
    )
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "parse_safari",
        lambda _: [{"domain": ".claude.ai", "name": "sessionKey", "value": "synthetic-safari"}],
    )
    candidates = sidecar.BrowserCookieExtractor.get_macos_candidates(
        "anthropic", ["claude.ai"], "sessionKey"
    )
    assert candidates == [
        (
            f"synthetic-{browser_type}",
            f"cookie:anthropic/{browser_type.title()}/Default/cookies.sqlite",
        )
    ]


def test_legacy_console_cookie_cannot_become_subscription(tmp_path, monkeypatch, helper):
    path = tmp_path / "Cookies"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('version', '24')")
        conn.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, expires_utc INTEGER)"
        )
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, 0)",
            (".console.anthropic.com", "sessionKey", encrypted(".console.anthropic.com")),
        )
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "get_all_paths",
        lambda: [{"browser": "Chrome", "type": "chromium", "path": path}],
    )
    assert (
        sidecar.BrowserCookieExtractor.get_macos_candidates(
            "anthropic", ["anthropic.com", "claude.ai"], "sessionKey"
        )
        == []
    )
    assert (
        len(
            sidecar.BrowserCookieExtractor.get_macos_candidates(
                "anthropic_console", ["console.anthropic.com"], "sessionKey"
            )
        )
        == 1
    )
