"""Synthetic Chromium stores; no real browser or Keychain access."""

import json
import sqlite3
import subprocess
from unittest.mock import Mock

import pytest

from scripts import sidecar
from scripts.sidecar_pkg import macos_keychain_store as keychain
from scripts.sidecar_pkg.macos_keychain_store import KeychainError
from tests.macos_cookie_fixtures import approved_keychain_readers, encrypted  # noqa: F401


@pytest.fixture
def helper(monkeypatch):
    from scripts.sidecar_pkg import macos_cookies

    monkeypatch.setattr(macos_cookies, "_KEY_CACHE", {})
    lookup = Mock(return_value=(b"synthetic-key"))
    monkeypatch.setattr(keychain, "read_secret", lookup)
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
    [KeychainError("missing"), KeychainError("timed_out")],
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
    lookup = Mock(side_effect=KeychainError("missing"))
    monkeypatch.setattr(keychain, "read_secret", lookup)
    monkeypatch.setattr(macos_cookies.time, "monotonic", lambda: 0)
    assert claude_desktop._storage_key(tmp_path) is None
    monkeypatch.setattr(macos_cookies.time, "monotonic", lambda: 10000)
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
    if browser_type == "safari":
        from tests.macos_cookie_fixtures import safari_blob

        path.write_bytes(safari_blob(value="synthetic-safari"))
    candidates = sidecar.BrowserCookieExtractor.get_macos_candidates(
        "anthropic", ["claude.ai"], "sessionKey"
    )
    assert len(candidates) == 1
    assert candidates[0][0] == f"synthetic-{browser_type}"
    assert candidates[0][1].startswith(
        f"cookie:anthropic/{browser_type.title()}/Default/cookies.sqlite#"
    )


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


def test_different_console_sessions_in_same_store_have_distinct_final_origins(
    tmp_path, monkeypatch, helper
):
    path = tmp_path / "Cookies"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('version', '24')")
        conn.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, expires_utc INTEGER)"
        )
        for host, value in [
            (".platform.claude.com", b"session-one"),
            (".platform.claude.com", b"session-two"),
            (".console.anthropic.com", b"session-three"),
        ]:
            conn.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, 0)",
                (host, "sessionKey", encrypted(host, value)),
            )
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "get_all_paths",
        lambda: [{"browser": "Chrome", "type": "chromium", "path": path}],
    )
    candidates = sidecar.BrowserCookieExtractor.get_macos_candidates(
        "anthropic_console", ["platform.claude.com", "console.anthropic.com"], "sessionKey"
    )
    assert len(candidates) == 3
    assert len({origin for _, origin in candidates}) == 3
    assert all("#" in origin and value not in origin for value, origin in candidates)


@pytest.mark.parametrize(
    "provider,field", [("anthropic", "cookie_sessionKey"), ("anthropic_console", "session_cookie")]
)
def test_cookie_rotation_cannot_inherit_base_or_provider_tag(monkeypatch, provider, field):
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    base = f"cookie:{provider}/Chrome/Default/Cookies"
    origin = base + "#0123456789ab"
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor, "get_macos_candidates", lambda *_: [("new-session", origin)]
    )
    config = {
        "rules": [
            {
                "type": "cookie",
                "name": "sessionKey",
                "domains": ["claude.ai"] if provider == "anthropic" else ["console.anthropic.com"],
                "mapping": {"value": field},
            }
        ]
    }
    cards, blocked = sidecar.GenericCollector.collect_provider(
        provider,
        config,
        account_label_hints={
            provider: {base: "old@example.com", f"provider:{provider}": "old@example.com"}
        },
    )
    assert len(cards) == 1
    assert cards[0]["metadata"]["identity_pending"] is True
    assert cards[0]["account_id"] is None
    assert blocked
    tagged, blocked = sidecar.GenericCollector.collect_provider(
        provider, config, account_label_hints={provider: {origin: "new@example.com"}}
    )
    assert tagged[0]["account_id"] == "new@example.com"
    assert blocked == []


@pytest.mark.parametrize("failure", ["iterdir", "stat"])
def test_unreadable_browser_store_does_not_hide_healthy_browser(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sidecar.Path, "home", classmethod(lambda cls: tmp_path))
    chrome = tmp_path / "Library/Application Support/Google/Chrome"
    edge = tmp_path / "Library/Application Support/Microsoft Edge"
    for base in [chrome, edge]:
        path = base / "Default/Network/Cookies"
        path.parent.mkdir(parents=True)
        path.touch()
    original_iterdir = type(chrome).iterdir
    original_stat = type(chrome).stat

    def guarded_iterdir(path):
        if path == chrome and failure == "iterdir":
            raise PermissionError("synthetic inaccessible browser")
        return original_iterdir(path)

    def guarded_stat(path, *args, **kwargs):
        if path == chrome / "Default/Network/Cookies" and failure == "stat":
            raise PermissionError("synthetic disappearing store")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(type(chrome), "iterdir", guarded_iterdir)
    monkeypatch.setattr(type(chrome), "stat", guarded_stat)
    paths = sidecar.BrowserCookieExtractor.get_all_paths()
    healthy = [target for target in paths if target["browser"] == "Edge"]
    assert len(healthy) == 1
    assert healthy[0]["path"] == edge / "Default/Network/Cookies"
    assert healthy[0]["profile"] == "Default"
    assert healthy[0]["store"] == "Network/Cookies"
    assert not any(target["browser"] == "Chrome" for target in paths)


def test_desktop_session_rotation_has_distinct_final_origins(monkeypatch):
    from scripts.sidecar_pkg import claude_desktop

    values = iter(["desktop-session-one", "desktop-session-two"])
    from scripts.sidecar_pkg.cookie_scan import StoreScan

    monkeypatch.setattr(
        claude_desktop, "scan_session_cookie", lambda _: StoreScan("candidates", [next(values)])
    )
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [])
    first = sidecar.BrowserCookieExtractor.get_macos_candidates(
        "anthropic", ["claude.ai"], "sessionKey"
    )
    second = sidecar.BrowserCookieExtractor.get_macos_candidates(
        "anthropic", ["claude.ai"], "sessionKey"
    )
    assert first[0][1] != second[0][1]
    assert first[0][1].startswith("cookie:anthropic/Claude Desktop/Default/Cookies#")
    assert second[0][1].startswith("cookie:anthropic/Claude Desktop/Default/Cookies#")
