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
from scripts.sidecar_pkg import macos_keychain_store as keychain
from scripts.sidecar_pkg.macos_keychain_store import KeychainError
from tests.macos_cookie_fixtures import (
    FIXED_EXPIRY,
    approved_keychain_readers,  # noqa: F401
    chromium_store,
    encrypted,
    safari_blob,
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(macos_cookies, "_KEY_CACHE", {})

    def forbidden(*args, **kwargs):
        raise AssertionError("Native credential access forbidden")

    monkeypatch.setattr(subprocess, "run", forbidden)


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
    "bad",
    [
        b"cook",
        b"cook" + struct.pack(">I", 0xFFFFFFFF),
        b"wrong",
        # A fixed expiry keeps the blob's bytes -- and therefore this test's
        # id -- reproducible; the default expiry comes from the clock, which
        # gave every process a different id and broke pytest-xdist.
        pytest.param(safari_blob(expiry=FIXED_EXPIRY)[:-2], id="truncated-blob"),
    ],
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
        (KeychainError("timed_out"), "timed_out"),
        (KeychainError("denied"), "denied"),
        (KeychainError("missing"), "missing"),
        (KeychainError("unavailable"), "inaccessible"),
    ],
)
def test_failed_keychain_outcomes_are_cached_without_repeated_prompts(
    tmp_path, monkeypatch, failure, status
):

    path = tmp_path / "Cookies"
    chromium_store(path, [(".claude.ai", encrypted(".claude.ai"), 0)])
    calls = []

    def lookup(*args, **kwargs):
        calls.append(True)
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(keychain, "read_secret", lookup)
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
        return b"synthetic-key"

    monkeypatch.setattr(keychain, "read_secret", lookup)
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

    path = tmp_path / "Cookies"
    chromium_store(path, [(host, prefix + encrypted(host)[3:], 0)])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: b"synthetic-key")
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


@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("unrelated", [False, True])
def test_safari_partial_records_preserve_independent_credentials(tmp_path, bad_first, unrelated):
    from scripts.sidecar_pkg.safari_cookies import scan_cookies

    good = safari_blob()
    bad = safari_blob(domain="example.com" if unrelated else ".claude.ai")
    bad = bad.replace(b"synthetic-session", b"\xff" + b"x" * 16)
    pages = [good[12:], bad[12:]]
    if bad_first:
        pages.reverse()
    path = tmp_path / "Cookies.binarycookies"
    path.write_bytes(b"cook" + struct.pack(">III", 2, *map(len, pages)) + b"".join(pages))
    result = scan_cookies(path, "claude.ai", "sessionKey")
    assert [item["value"] for item in result.values] == ["synthetic-session"]
    assert result.status == ("candidates" if unrelated else "unsupported")
    assert result.rejected_count == (0 if unrelated else 1)


@pytest.mark.parametrize("reverse", [False, True])
def test_chromium_denial_outcome_does_not_depend_on_row_order(tmp_path, monkeypatch, reverse):
    path = tmp_path / "Cookies"
    rows = [(".claude.ai", encrypted(".claude.ai"), 0), (".claude.ai", b"v20bad", 0)]
    chromium_store(path, rows[::-1] if reverse else rows)

    def deny(*args, **kwargs):
        raise KeychainError("denied")

    monkeypatch.setattr(keychain, "read_secret", deny)
    result = macos_cookies.scan_cookies(path, "Chrome Safe Storage", "claude.ai", "sessionKey")
    assert result.status == "denied"
    assert result.rejected_count == 2


def test_schema_diagnostic_reports_version_without_store_identity(tmp_path, caplog):
    from scripts.sidecar_pkg.cookie_scan import report_scan

    path = tmp_path / "Cookies"
    chromium_store(path, version=999)
    result = macos_cookies.scan_cookies(path, "Chrome Safe Storage", "claude.ai", "sessionKey")
    outcomes = []
    report_scan("Chrome", "private-profile", result, outcomes)
    assert outcomes[0]["schema_version"] == 999
    assert "private-profile" not in json.dumps(outcomes)


def test_diagnostic_store_ids_are_process_keyed(monkeypatch):
    from scripts.sidecar_pkg import cookie_scan

    outcomes = []
    for _ in range(2):
        cookie_scan.report_scan(
            "Safari", "private-profile", cookie_scan.StoreScan("empty"), outcomes
        )
    assert outcomes[0]["store_id"] == outcomes[1]["store_id"]
    monkeypatch.setattr(cookie_scan, "_DIAGNOSTIC_KEY", b"disposable-test-key")
    cookie_scan.report_scan("Safari", "private-profile", cookie_scan.StoreScan("empty"), outcomes)
    assert outcomes[0]["store_id"] != outcomes[2]["store_id"]


def test_scan_logging_keeps_store_details_at_debug_and_info_aggregated(caplog):
    import logging

    from scripts.sidecar_pkg.cookie_scan import StoreScan, report_scan, report_scan_summary

    outcomes = []
    with caplog.at_level(logging.DEBUG):
        report_scan("Safari", "private-profile", StoreScan("empty", expired_count=2), outcomes)
        report_scan(
            "Chrome", "private-profile-two", StoreScan("denied", rejected_count=1), outcomes
        )
        report_scan_summary(outcomes)
    info = [record.message for record in caplog.records if record.levelno == logging.INFO]
    assert len(info) == 1
    assert "scans=2" in info[0]
    assert "'denied': 1" in info[0] and "'empty': 1" in info[0]
    assert "expired=2 rejected=1" in info[0]
    assert "store_id" not in info[0]
    assert "private-profile" not in caplog.text


def test_legacy_desktop_cookie_path_reports_scan_outcome(tmp_path, monkeypatch, caplog):
    import logging

    from scripts.sidecar_pkg import claude_desktop
    from scripts.sidecar_pkg.cookie_scan import StoreScan

    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", raising=False)
    monkeypatch.setattr(
        claude_desktop,
        "scan_session_cookie",
        lambda path: StoreScan("candidates", ["synthetic-session"]),
    )
    with caplog.at_level(logging.INFO):
        assert (
            sidecar.BrowserCookieExtractor.get_cookie("claude.ai", "sessionKey")
            == "synthetic-session"
        )
    assert "macOS cookie scan summary: scans=1" in caplog.text
    assert "synthetic-session" not in caplog.text
