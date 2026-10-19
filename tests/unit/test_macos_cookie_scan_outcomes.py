"""Synthetic native-store outcomes; never read the host home or credential store."""

import json
import sqlite3
import struct
import subprocess
import time
from pathlib import Path

import pytest

from scripts import sidecar
from scripts.sidecar_pkg import macos_cookies


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(macos_cookies, "_KEY_CACHE", {})

    def forbidden(*args, **kwargs):
        raise AssertionError("Native credential access forbidden")

    monkeypatch.setattr(subprocess, "run", forbidden)


def safari_blob(*, expiry=None, domain=".claude.ai", value="synthetic-session"):
    strings = [domain, "sessionKey", "/", value]
    offsets, tail = [], b""
    for text in strings:
        offsets.append(56 + len(tail))
        tail += text.encode() + b"\0"
    record = struct.pack("<4I4I", 56 + len(tail), 0, 0, 0, *offsets)
    record += b"\0" * 8
    record += struct.pack(
        "<dd", (expiry if expiry is not None else time.time() + 3600) - 978307200, 1
    )
    record += tail
    page = b"\0\0\1\0" + struct.pack("<II", 1, 16) + b"\0" * 4 + record
    return b"cook" + struct.pack(">II", 1, len(page)) + page


def test_safari_expiry_is_filtered_by_existing_candidate_interface(tmp_path, monkeypatch):
    path = tmp_path / "Cookies.binarycookies"
    path.write_bytes(safari_blob(expiry=time.time() - 10))
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "get_all_paths",
        lambda: [{"browser": "Safari", "type": "safari", "path": path, "profile": "Default"}],
    )
    monkeypatch.setenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "0")
    assert (
        sidecar.BrowserCookieExtractor.get_macos_candidates(
            "anthropic", ["claude.ai"], "sessionKey"
        )
        == []
    )


@pytest.mark.parametrize(
    "bad", [b"cook", b"cook" + struct.pack(">I", 0xFFFFFFFF), b"wrong", safari_blob()[:-2]]
)
def test_safari_malformed_is_unsupported_not_empty(tmp_path, bad):
    from scripts.sidecar_pkg.safari_cookies import scan_cookies

    path = tmp_path / "Cookies.binarycookies"
    path.write_bytes(bad)
    result = scan_cookies(path)
    assert result.status == "unsupported"
    assert result.values == []


def test_safari_distinguishes_empty_missing_and_expired(tmp_path):
    from scripts.sidecar_pkg.safari_cookies import scan_cookies

    path = tmp_path / "Cookies.binarycookies"
    assert scan_cookies(path).status == "missing"
    path.write_bytes(b"cook" + struct.pack(">I", 0))
    assert scan_cookies(path).status == "empty"
    path.write_bytes(safari_blob(expiry=time.time() - 10))
    result = scan_cookies(path)
    assert result.status == "empty"
    assert result.expired_count == 1
    path.write_bytes(safari_blob())
    result = scan_cookies(path)
    assert result.status == "candidates"
    assert result.values[0]["value"] == "synthetic-session"
    assert "synthetic-session" not in repr(result)


def test_safari_profile_discovery_includes_container_and_distinct_profiles(tmp_path):
    from scripts.sidecar_pkg.safari_cookies import stores

    rels = [
        "Library/Cookies/Cookies.binarycookies",
        "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
        "Library/Containers/com.apple.Safari/Data/Library/WebKit/WebsiteDataStore/profile-one/Cookies/Cookies.binarycookies",
        "Library/Containers/com.apple.Safari/Data/Library/WebKit/WebsiteDataStore/profile-two/Cookies/Cookies.binarycookies",
    ]
    for rel in rels:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(safari_blob())
    found = stores(tmp_path)
    assert {p["path"] for p in found} == {tmp_path / rel for rel in rels}
    assert len({(p["profile"], p["store"]) for p in found}) == 4


def chromium_store(path, rows=(), version=24):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
        conn.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, expires_utc INTEGER)"
        )
        conn.executemany("INSERT INTO cookies VALUES (?, 'sessionKey', ?, ?)", rows)


def test_chromium_outcomes_do_not_treat_missing_or_unsupported_as_empty(tmp_path):
    path = tmp_path / "Cookies"
    assert (
        macos_cookies.scan_cookies(path, "Chrome Safe Storage", "claude.ai", "sessionKey").status
        == "missing"
    )
    chromium_store(path)
    assert (
        macos_cookies.scan_cookies(path, "Chrome Safe Storage", "claude.ai", "sessionKey").status
        == "empty"
    )
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value='999'")
    assert (
        macos_cookies.scan_cookies(path, "Chrome Safe Storage", "claude.ai", "sessionKey").status
        == "unsupported"
    )


@pytest.mark.parametrize(
    "failure,status",
    [
        (subprocess.TimeoutExpired("security", 30), "timed_out"),
        (subprocess.CompletedProcess([], 128, "", ""), "denied"),
        (subprocess.CompletedProcess([], 44, "", ""), "missing"),
        (subprocess.CompletedProcess([], 36, "", ""), "inaccessible"),
    ],
)
def test_failed_keychain_outcomes_are_cached_without_repeated_prompts(
    tmp_path, monkeypatch, failure, status
):
    from tests.unit.test_macos_browser_cookies import encrypted

    path = tmp_path / "Cookies"
    chromium_store(path, [(".claude.ai", encrypted(".claude.ai"), 0)])
    calls = []

    def lookup(*args, **kwargs):
        calls.append(True)
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(subprocess, "run", lookup)
    for _ in range(2):
        result = macos_cookies.scan_cookies(path, "Chrome Safe Storage", "claude.ai", "sessionKey")
        assert result.status == status
        assert result.values == []
    assert len(calls) == 1


def test_permission_error_is_inaccessible_without_leaking_path(tmp_path, monkeypatch, caplog):
    from scripts.sidecar_pkg.safari_cookies import scan_cookies

    path = tmp_path / "private-account/Cookies.binarycookies"
    monkeypatch.setattr(
        Path,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("private-account")),
    )
    assert scan_cookies(path).status == "inaccessible"
    assert "private-account" not in caplog.text


def test_desktop_cookie_scan_uses_shared_key_cache_and_reports_expired(tmp_path, monkeypatch):
    from scripts.sidecar_pkg import claude_desktop
    from tests.unit.test_macos_browser_cookies import encrypted

    path = tmp_path / "Cookies"
    chromium_store(path, [(".claude.ai", encrypted(".claude.ai"), 1)])
    result = claude_desktop.scan_session_cookie(path)
    assert result.status == "empty"
    assert result.expired_count == 1
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE cookies SET expires_utc=0")
    calls = []

    def lookup(*args, **kwargs):
        calls.append(True)
        return subprocess.CompletedProcess([], 0, "synthetic-key", "")

    monkeypatch.setattr(subprocess, "run", lookup)
    assert claude_desktop._storage_key(path)
    assert claude_desktop.scan_session_cookie(path).values == ["synthetic-session"]
    assert len(calls) == 1


def test_candidate_scan_reports_protected_safari_as_inaccessible(tmp_path, monkeypatch):
    path = tmp_path / "private-account/Cookies.binarycookies"
    monkeypatch.setenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "0")
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "get_all_paths",
        lambda: [{"type": "safari", "browser": "Safari", "path": path}],
    )
    original = Path.open

    def guarded(p, *args, **kwargs):
        if p == path:
            raise PermissionError("private-account")
        return original(p, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    outcomes = []
    assert (
        sidecar.BrowserCookieExtractor.get_macos_candidates(
            "anthropic", ["claude.ai"], "sessionKey", scan_outcomes=outcomes
        )
        == []
    )
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "inaccessible"
    assert outcomes[0]["candidate_count"] == 0
    assert "private-account" not in json.dumps(outcomes)


def test_safari_reports_empty_when_only_unrelated_cookies_exist(tmp_path, monkeypatch):
    path = tmp_path / "Cookies.binarycookies"
    path.write_bytes(safari_blob(domain="evil-claude.ai"))
    monkeypatch.setenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "0")
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "get_all_paths",
        lambda: [{"type": "safari", "browser": "Safari", "path": path}],
    )
    outcomes = []
    assert (
        sidecar.BrowserCookieExtractor.get_macos_candidates(
            "anthropic", ["claude.ai"], "sessionKey", scan_outcomes=outcomes
        )
        == []
    )
    assert outcomes[0]["status"] == "empty"
    assert outcomes[0]["candidate_count"] == 0


@pytest.mark.parametrize(
    "offset,value", [(16, 1), (20, 0xFFFFFFFF), (40, float("nan")), (48, float("inf"))]
)
def test_safari_rejects_invalid_offsets_and_nonfinite_dates(tmp_path, offset, value):
    from scripts.sidecar_pkg.safari_cookies import scan_cookies

    blob = bytearray(safari_blob())
    # File header 12, page header/table 16, then record.
    struct.pack_into("<d" if offset >= 40 else "<I", blob, 28 + offset, value)
    path = tmp_path / "Cookies.binarycookies"
    path.write_bytes(blob)
    assert scan_cookies(path).status == "unsupported"


@pytest.mark.parametrize("host,prefix", [(".other.claude.ai", b"v10"), (".claude.ai", b"v11")])
def test_desktop_preserves_exact_host_and_observed_cipher_allowlist(
    tmp_path, monkeypatch, host, prefix
):
    from scripts.sidecar_pkg import claude_desktop
    from tests.unit.test_macos_browser_cookies import encrypted

    path = tmp_path / "Cookies"
    chromium_store(path, [(host, prefix + encrypted(host)[3:], 0)])
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, "synthetic-key", "")
    )
    assert claude_desktop.read_session_cookie(path) is None


def test_safari_legacy_origin_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "0")
    path = tmp_path / "Library/Cookies/Cookies.binarycookies"
    path.parent.mkdir(parents=True)
    path.write_bytes(safari_blob())
    candidates = sidecar.BrowserCookieExtractor.get_macos_candidates(
        "anthropic", ["claude.ai"], "sessionKey"
    )
    assert candidates[0][1].startswith("cookie:anthropic/Safari/Cookies/Cookies.binarycookies#")


def test_locked_disposable_chromium_store_is_inaccessible(tmp_path):
    path = tmp_path / "Cookies"
    chromium_store(path)
    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        assert (
            macos_cookies.scan_cookies(
                path, "Chrome Safe Storage", "claude.ai", "sessionKey"
            ).status
            == "inaccessible"
        )


def test_safari_case_insensitive_domain_matching(tmp_path, monkeypatch):
    path = tmp_path / "Cookies.binarycookies"
    path.write_bytes(safari_blob(domain=".CLAUDE.AI"))
    monkeypatch.setenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "0")
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "get_all_paths",
        lambda: [{"type": "safari", "browser": "Safari", "path": path}],
    )
    assert (
        len(
            sidecar.BrowserCookieExtractor.get_macos_candidates(
                "anthropic", ["claude.ai"], "sessionKey"
            )
        )
        == 1
    )


@pytest.mark.parametrize("layout", ["no_tables", "no_cookies", "missing_columns"])
def test_readable_sqlite_with_unsupported_layout_is_not_inaccessible(tmp_path, layout):
    path = tmp_path / "Cookies"
    with sqlite3.connect(path) as conn:
        if layout != "no_tables":
            conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
            conn.execute("INSERT INTO meta VALUES ('version', '24')")
        if layout == "missing_columns":
            conn.execute("CREATE TABLE cookies (name TEXT)")
    assert (
        macos_cookies.scan_cookies(path, "Chrome Safe Storage", "claude.ai", "sessionKey").status
        == "unsupported"
    )


def test_protected_safari_profile_enumeration_reports_inaccessible(tmp_path, monkeypatch):
    from scripts.sidecar_pkg.safari_cookies import stores

    root = tmp_path / "Library/Containers/com.apple.Safari/Data/Library/WebKit/WebsiteDataStore"
    original = Path.iterdir

    def guarded(path):
        if path == root:
            raise PermissionError("private profile tree")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", guarded)
    targets = stores(tmp_path)
    assert len(targets) == 1
    assert targets[0]["discovery_status"] == "inaccessible"
