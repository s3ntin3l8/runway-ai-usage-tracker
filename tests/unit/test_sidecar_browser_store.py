"""Browser cookie stores are classified once per cycle, not once per cookie name."""

import logging
import os
import sqlite3
from pathlib import Path

import pytest

from scripts import sidecar
from scripts.sidecar_pkg import browser_store
from scripts.sidecar_pkg.browser_store import StoreState


@pytest.fixture(autouse=True)
def _clean_cycle():
    """Each test starts from an empty cycle and leaves none behind."""
    browser_store.reset_store_probe_cache()
    sidecar.reset_cookie_store_tracking()
    yield
    browser_store.reset_store_probe_cache()
    sidecar.reset_cookie_store_tracking()


@pytest.fixture(autouse=True)
def _isolate_host_stores(tmp_path, monkeypatch):
    """Synthetic tests must never inspect the host's real browser stores."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Windows")


def _target(path, **overrides):
    target = {
        "browser": "Chrome",
        "type": "chromium",
        "path": path,
        "profile": "Default",
        "store": "Network/Cookies",
    }
    target.update(overrides)
    return target


def _chromium_store(path, blobs):
    """A Chromium cookie store whose encrypted_value blobs are `blobs`."""
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
        for name, blob in blobs:
            conn.execute("INSERT INTO cookies VALUES (?, ?, ?)", (".example.com", name, blob))
    return path


def _firefox_store(path):
    """A Firefox cookie store: plaintext values, no encryption."""
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
        conn.execute("INSERT INTO moz_cookies VALUES ('.example.com', 'sid', 'plaintext-session')")
    return path


class _Refused(sqlite3.OperationalError):
    """A sqlite open failure that reports SQLITE_CANTOPEN on every platform.

    A directory is the usual stand-in for a file the OS refuses to open, but
    sqlite reports it differently per platform: Windows says "unable to open
    database file" (SQLITE_CANTOPEN) and Linux says "disk I/O error"
    (SQLITE_IOERR). Provoking the failure therefore would exercise a different
    code path per platform, so the failure is injected instead.
    """

    @property
    def sqlite_errorcode(self) -> int:
        return sqlite3.SQLITE_CANTOPEN


class _SqliteProxy:
    """The real sqlite3 module with ``connect`` replaced.

    Only ``browser_store.sqlite3`` is patched, so the module's own use of
    sqlite3 stays untouched — and so does every other module's.
    """

    def __init__(self, connect):
        # An instance attribute, so ``connect`` is never looked up on the real
        # module by ``__getattr__``.
        self.connect = connect

    def __getattr__(self, name):
        return getattr(sqlite3, name)


def _refuses_to_open(monkeypatch):
    """Make every sqlite open of a store fail the way a sharing violation does."""

    def _connect(*args, **kwargs):
        raise _Refused("unable to open database file")

    monkeypatch.setattr(browser_store, "sqlite3", _SqliteProxy(_connect))


def _held_open(monkeypatch):
    """Report the store as held open by another process, as Win32 would."""
    monkeypatch.setattr(
        browser_store,
        "_win32_access_state",
        lambda target: (StoreState.LOCKED, "held open by another process"),
    )


def test_a_missing_store_is_reported_as_missing(tmp_path):
    probe = browser_store.probe_store(_target(tmp_path / "Cookies"))
    assert probe.state is StoreState.MISSING


def test_a_file_that_is_not_sqlite_is_reported_as_not_sqlite(tmp_path):
    path = tmp_path / "Cookies"
    path.write_bytes(b"not a sqlite database at all")
    probe = browser_store.probe_store(_target(path))
    assert probe.state is StoreState.NOT_SQLITE


def test_a_chromium_store_with_v10_values_is_readable(tmp_path):
    path = _chromium_store(tmp_path / "Cookies", [("session", b"v10" + b"\x01" * 32)])
    probe = browser_store.probe_store(_target(path))
    assert probe.state is StoreState.READABLE
    assert probe.readable is True


def test_a_chromium_store_with_v20_values_is_reported_as_unsupported(tmp_path):
    path = _chromium_store(tmp_path / "Cookies", [("session", b"v20" + b"\x01" * 32)])
    probe = browser_store.probe_store(_target(path))
    assert probe.state is StoreState.UNSUPPORTED_FORMAT
    # The file still opens, so a store mixing v10 and v20 keeps the values
    # that can still be decrypted.
    assert probe.readable is True


def test_a_firefox_store_is_readable(tmp_path):
    path = _firefox_store(tmp_path / "cookies.sqlite")
    probe = browser_store.probe_store(
        _target(path, browser="Firefox", type="firefox", store="cookies.sqlite")
    )
    assert probe.state is StoreState.READABLE


def test_a_store_is_probed_once_per_cycle(tmp_path):
    path = _chromium_store(tmp_path / "Cookies", [("session", b"v10" + b"\x01" * 32)])
    first = browser_store.probe_store(_target(path))
    second = browser_store.probe_store(_target(path))
    assert first is second


def test_a_new_cycle_reprobes_the_store(tmp_path):
    path = _chromium_store(tmp_path / "Cookies", [("session", b"v10" + b"\x01" * 32)])
    first = browser_store.probe_store(_target(path))
    browser_store.reset_store_probe_cache()
    assert browser_store.probe_store(_target(path)) is not first


def test_a_sqlite_lock_is_read_from_a_copy(tmp_path):
    """Firefox holds its store with a SQLite lock; the file stays shareable."""
    path = _firefox_store(tmp_path / "cookies.sqlite")
    holder = sqlite3.connect(path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        probe = browser_store.probe_store(
            _target(path, browser="Firefox", type="firefox", store="cookies.sqlite")
        )
        assert probe.state is StoreState.READABLE
        assert probe.read_path is not None
        connection = browser_store.open_store(probe)
        assert connection is not None
        try:
            row = connection.execute("SELECT value FROM moz_cookies WHERE name = 'sid'").fetchone()
            assert row[0] == "plaintext-session"
        finally:
            connection.close()
    finally:
        holder.rollback()
        holder.close()


def test_a_sharing_violation_is_not_retried_with_a_copy(tmp_path, monkeypatch):
    """A file the OS refuses to open cannot be copied either."""
    path = tmp_path / "Cookies"
    path.touch()
    _refuses_to_open(monkeypatch)
    _held_open(monkeypatch)
    copies = []
    monkeypatch.setattr(browser_store, "_copy_store", lambda target: copies.append(target))

    probe = browser_store.probe_store(_target(path))

    assert probe.state is StoreState.LOCKED
    assert copies == []


def test_a_store_the_os_refuses_to_open_is_unreadable_without_a_win32_verdict(
    tmp_path, monkeypatch
):
    """Only Win32 can tell a sharing violation from an ordinary refusal.

    Elsewhere a store that will not open has no known reason, so it is
    reported as unreadable rather than as held open by another process.
    """
    path = tmp_path / "Cookies"
    path.touch()
    _refuses_to_open(monkeypatch)
    monkeypatch.setattr(browser_store, "_win32_access_state", lambda target: (None, ""))

    probe = browser_store.probe_store(_target(path))

    assert probe.state is StoreState.UNREADABLE


def test_the_win32_probe_answers_nothing_on_other_platforms(tmp_path, monkeypatch):
    """POSIX has no sharing violation to detect, so the probe stays silent."""
    monkeypatch.setattr(browser_store.sys, "platform", "linux")
    assert browser_store._win32_access_state(tmp_path / "Cookies") == (None, "")


def test_a_locked_store_is_skipped_without_a_warning(tmp_path, monkeypatch, caplog):
    path = tmp_path / "Cookies"
    path.touch()
    _refuses_to_open(monkeypatch)
    _held_open(monkeypatch)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [_target(path)])

    with caplog.at_level(logging.WARNING):
        for name in ("session", "auth", "kimi-auth"):
            sidecar.BrowserCookieExtractor.get_cookie(
                "example.com", name, "ollama", "session_cookie"
            )

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_locked_store_is_reported_once_per_cycle(tmp_path, monkeypatch, caplog):
    path = tmp_path / "Cookies"
    path.touch()
    _refuses_to_open(monkeypatch)
    _held_open(monkeypatch)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [_target(path)])

    with caplog.at_level(logging.WARNING):
        for name in ("session", "auth", "kimi-auth"):
            sidecar.BrowserCookieExtractor.get_cookie(
                "example.com", name, "ollama", "session_cookie"
            )
        sidecar.report_cookie_stores()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "Chrome/Default/Network/Cookies" in message
    assert "held open by another process" in message


def test_the_report_names_the_environment_variable_that_would_work(tmp_path, monkeypatch, caplog):
    path = tmp_path / "Cookies"
    path.touch()
    _refuses_to_open(monkeypatch)
    _held_open(monkeypatch)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [_target(path)])

    with caplog.at_level(logging.WARNING):
        sidecar.BrowserCookieExtractor.get_cookie(
            "ollama.com", "session", "ollama", "session_cookie"
        )
        sidecar.report_cookie_stores()

    assert "OLLAMA_SESSION_TOKEN" in caplog.text


def test_a_locked_store_whose_copy_is_unreadable_reports_why(tmp_path, monkeypatch):
    """A copy that cannot be read must not be reported as a lock."""
    path = _firefox_store(tmp_path / "cookies.sqlite")
    holder = sqlite3.connect(path)
    holder.execute("BEGIN EXCLUSIVE")
    broken = tmp_path / "broken.sqlite"
    broken.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(browser_store, "_copy_store", lambda target: broken)
    try:
        probe = browser_store.probe_store(
            _target(path, browser="Firefox", type="firefox", store="cookies.sqlite")
        )
        assert probe.state is StoreState.NOT_SQLITE
    finally:
        holder.rollback()
        holder.close()


def test_the_blocked_providers_are_forgotten_between_cycles(tmp_path, monkeypatch):
    path = tmp_path / "Cookies"
    path.touch()
    _refuses_to_open(monkeypatch)
    _held_open(monkeypatch)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [_target(path)])

    sidecar.BrowserCookieExtractor.get_cookie("ollama.com", "session", "ollama", "session_cookie")
    assert sidecar._cookie_store_blocked

    sidecar.reset_cookie_store_tracking()

    assert sidecar._cookie_store_blocked == {}


def test_a_firefox_store_is_read_through_the_probe(tmp_path, monkeypatch):
    path = _firefox_store(tmp_path / "cookies.sqlite")
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor,
        "get_all_paths",
        lambda: [_target(path, browser="Firefox", type="firefox", store="cookies.sqlite")],
    )
    assert sidecar.BrowserCookieExtractor.get_cookie("example.com", "sid") == "plaintext-session"


def test_windows_chromium_stores_carry_their_profile_and_store(tmp_path):
    base = tmp_path / "AppData/Local/Google/Chrome/User Data"
    for relative in ("Default/Network/Cookies", "Profile 1/Network/Cookies"):
        path = base / relative
        path.parent.mkdir(parents=True)
        path.touch()
    paths = sidecar.BrowserCookieExtractor.get_all_paths()
    chromium = {
        target["profile"]: target["store"] for target in paths if target["browser"] == "Chrome"
    }
    assert chromium == {"Default": "Network/Cookies", "Profile 1": "Network/Cookies"}


def test_linux_chromium_stores_carry_their_profile_and_store(tmp_path, monkeypatch):
    """Every Chromium store is named, wherever it lives on disk."""
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    flatpak = tmp_path / ".var/app/org.chromium.Chromium/config/chromium"
    snap = tmp_path / "snap/chromium/common/chromium"
    for base, relative in (
        (flatpak, "Default/Network/Cookies"),
        (snap, "Profile 1/Cookies"),
    ):
        path = base / relative
        path.parent.mkdir(parents=True)
        path.touch()

    named = {
        target["browser"]: (target["profile"], target["store"])
        for target in sidecar.BrowserCookieExtractor.get_all_paths()
        if target["type"] == "chromium"
    }
    assert named == {
        "Chromium (Flatpak)": ("Default", "Network/Cookies"),
        "Chromium (Snap)": ("Profile 1", "Cookies"),
    }


def test_a_store_name_distinguishes_two_profiles_of_one_browser(tmp_path):
    base = tmp_path / "AppData/Local/Google/Chrome/User Data"
    for profile in ("Default", "Profile 1"):
        path = base / profile / "Network/Cookies"
        path.parent.mkdir(parents=True)
        path.touch()
    identities = {
        browser_store.probe_store(target).identity()
        for target in sidecar.BrowserCookieExtractor.get_all_paths()
        if target["browser"] == "Chrome"
    }
    assert identities == {
        "Chrome/Default/Network/Cookies",
        "Chrome/Profile 1/Network/Cookies",
    }


def test_the_cycle_report_is_silent_when_every_store_is_readable(tmp_path, caplog):
    path = _chromium_store(tmp_path / "Cookies", [("session", b"v10" + b"\x01" * 32)])
    browser_store.probe_store(_target(path))
    with caplog.at_level(logging.WARNING):
        sidecar.report_cookie_stores()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_the_copy_made_for_a_locked_store_is_removed_with_the_cycle(tmp_path):
    path = _firefox_store(tmp_path / "cookies.sqlite")
    holder = sqlite3.connect(path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        probe = browser_store.probe_store(
            _target(path, browser="Firefox", type="firefox", store="cookies.sqlite")
        )
        assert probe.read_path is not None
        assert probe.read_path.exists()
        browser_store.reset_store_probe_cache()
        assert not probe.read_path.exists()
    finally:
        holder.rollback()
        holder.close()


def test_the_probe_does_not_touch_a_store_that_does_not_exist(tmp_path):
    """A missing store is normal, not a failure to report."""
    probe = browser_store.probe_store(_target(tmp_path / "Cookies"))
    assert probe.state is StoreState.MISSING
    assert browser_store.unusable_stores() == []


def test_the_environment_lookup_only_matches_the_same_credential():
    assert sidecar._session_cookie_env_var("ollama", "session_cookie") == "OLLAMA_SESSION_TOKEN"
    assert sidecar._session_cookie_env_var("ollama", "not-a-real-mapping") is None
    assert sidecar._session_cookie_env_var("not-a-provider", "session_cookie") is None


def test_local_appdata_is_not_read_when_the_sidecar_runs_on_another_platform(tmp_path, monkeypatch):
    """The Windows store list must not leak into a macOS run."""
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    base = tmp_path / "AppData/Local/Google/Chrome/User Data"
    (base / "Default/Network").mkdir(parents=True)
    (base / "Default/Network/Cookies").touch()
    assert sidecar.BrowserCookieExtractor.get_all_paths() == []
    assert os.environ["LOCALAPPDATA"] == str(tmp_path / "AppData/Local")


# --- The report to the server ------------------------------------------------


def _locked_store(tmp_path, monkeypatch):
    """A store the browser holds open, so nothing can read it."""
    path = tmp_path / "Cookies"
    path.touch()
    _refuses_to_open(monkeypatch)
    _held_open(monkeypatch)
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [_target(path)])
    return path


def test_a_locked_store_is_reported_to_the_server(tmp_path, monkeypatch):
    _locked_store(tmp_path, monkeypatch)

    sidecar.BrowserCookieExtractor.get_cookie("ollama.com", "session", "ollama", "session_cookie")

    assert sidecar.blocked_source_entries() == [
        {
            "provider_id": "ollama",
            "credential_origin": "cookie:ollama/Chrome/Default/Network/Cookies",
            "reason": "source_locked",
            "source_store": "Chrome/Default/Network/Cookies",
            "source_detail": "held open by another process",
            "source_remedy": "set OLLAMA_SESSION_TOKEN (or paste the cookie in Settings → Providers)",
        }
    ]


def test_an_app_bound_store_is_reported_to_the_server(tmp_path, monkeypatch):
    """The store opened, so the probe does not skip it - the block is only visible
    where the decryption fails."""
    path = _chromium_store(tmp_path / "Cookies", [("session", b"v20" + b"\x01" * 32)])
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [_target(path)])
    monkeypatch.setattr(sidecar, "decrypt_chromium_cookie", lambda *a, **k: None)

    sidecar.BrowserCookieExtractor.get_cookie("example.com", "session", "ollama", "session_cookie")

    entries = sidecar.blocked_source_entries()
    assert [entry["reason"] for entry in entries] == ["source_unsupported"]
    assert entries[0]["source_detail"] == (
        "values are app-bound encrypted (v20) and need a browser-side read"
    )


def test_a_mixed_store_is_reported_when_the_provider_cookie_is_the_app_bound_one(
    tmp_path, monkeypatch
):
    """Chrome 127+ keeps pre-upgrade cookies as v10 and writes v20 only for the
    ones set afterwards, so the row the probe samples can be the decryptable
    one while the provider's own cookie is app-bound."""
    path = _chromium_store(
        tmp_path / "Cookies",
        [("old", b"v10" + b"\x01" * 32), ("session", b"v20" + b"\x01" * 32)],
    )
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [_target(path)])
    monkeypatch.setattr(sidecar, "decrypt_chromium_cookie", lambda *a, **k: None)

    # The probe samples the first row, which is still decryptable.
    assert browser_store.probe_store(_target(path)).state is StoreState.READABLE

    sidecar.BrowserCookieExtractor.get_cookie("example.com", "session", "ollama", "session_cookie")

    entries = sidecar.blocked_source_entries()
    assert [entry["reason"] for entry in entries] == ["source_unsupported"]
    assert entries[0]["source_detail"] == (
        "values are app-bound encrypted (v20) and need a browser-side read"
    )


def test_a_v10_failure_in_a_mixed_store_is_not_labelled_app_bound(tmp_path, monkeypatch):
    """A value that is not app-bound keeps its own story, even in a store that
    holds an app-bound one.

    Reclassifying the probe sticks for the rest of the cycle, so the label has
    to follow the value that actually failed: a v10 cookie that will not
    decrypt is not app-bound, and saying so would send the operator after the
    wrong fix.
    """
    path = _chromium_store(
        tmp_path / "Cookies",
        [("kimi-auth", b"v10" + b"\x01" * 32), ("session", b"v20" + b"\x01" * 32)],
    )
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [_target(path)])
    monkeypatch.setattr(sidecar, "decrypt_chromium_cookie", lambda *a, **k: None)

    # The app-bound cookie fails first and makes the store app-bound.
    sidecar.BrowserCookieExtractor.get_cookie("example.com", "session", "ollama", "session_cookie")
    # A different provider's v10 cookie then fails for another reason.
    sidecar.BrowserCookieExtractor.get_cookie(
        "example.com", "kimi-auth", "kimi_coding", "session_cookie"
    )

    entries = sidecar.blocked_source_entries()
    assert [entry["provider_id"] for entry in entries] == ["ollama"]
    assert entries[0]["reason"] == "source_unsupported"

    # The store is still reclassified for the log: it really does hold an
    # app-bound value, and the operator should hear about that.
    assert [probe.identity() for probe in browser_store.unusable_stores()] == [
        "Chrome/Default/Network/Cookies"
    ]


def test_a_readable_store_is_not_reported_to_the_server(tmp_path, monkeypatch):
    path = _chromium_store(tmp_path / "Cookies", [("session", b"v10" + b"\x01" * 32)])
    monkeypatch.setattr(sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [_target(path)])
    monkeypatch.setattr(sidecar, "decrypt_chromium_cookie", lambda *a, **k: "a-session-value")

    sidecar.BrowserCookieExtractor.get_cookie("example.com", "session", "ollama", "session_cookie")

    assert sidecar.blocked_source_entries() == []


def test_a_missing_store_is_not_reported_to_the_server(tmp_path, monkeypatch):
    """A browser without that profile is normal, not a fault to report."""
    monkeypatch.setattr(
        sidecar.BrowserCookieExtractor, "get_all_paths", lambda: [_target(tmp_path / "Cookies")]
    )

    sidecar.BrowserCookieExtractor.get_cookie("example.com", "session", "ollama", "session_cookie")

    assert sidecar.blocked_source_entries() == []


def test_the_report_carries_no_credential_material(tmp_path, monkeypatch):
    _locked_store(tmp_path, monkeypatch)

    sidecar.BrowserCookieExtractor.get_cookie("ollama.com", "session", "ollama", "session_cookie")

    entry = sidecar.blocked_source_entries()[0]
    assert set(entry) == {
        "provider_id",
        "credential_origin",
        "reason",
        "source_store",
        "source_detail",
        "source_remedy",
    }
    assert str(tmp_path) not in " ".join(entry.values())


def test_the_remedy_falls_back_to_the_paste(tmp_path, monkeypatch):
    """A provider the sidecar has no environment variable for gets the paste."""
    _locked_store(tmp_path, monkeypatch)

    sidecar.BrowserCookieExtractor.get_cookie(
        "anthropic.com", "sessionKey", "anthropic", "cookie_sessionKey"
    )

    entry = sidecar.blocked_source_entries()[0]
    assert entry["source_remedy"] == "paste the cookie in Settings → Providers"


def test_one_store_can_block_several_providers(tmp_path, monkeypatch):
    _locked_store(tmp_path, monkeypatch)

    sidecar.BrowserCookieExtractor.get_cookie("ollama.com", "session", "ollama", "session_cookie")
    sidecar.BrowserCookieExtractor.get_cookie(
        "kimi.com", "session", "kimi_coding", "session_cookie"
    )

    entries = sidecar.blocked_source_entries()
    assert [entry["provider_id"] for entry in entries] == ["kimi_coding", "ollama"]
    assert entries[0]["source_remedy"].startswith("set KIMI_AUTH_TOKEN")
    assert entries[1]["source_remedy"].startswith("set OLLAMA_SESSION_TOKEN")


def test_the_report_is_forgotten_between_cycles(tmp_path, monkeypatch):
    _locked_store(tmp_path, monkeypatch)

    sidecar.BrowserCookieExtractor.get_cookie("ollama.com", "session", "ollama", "session_cookie")
    assert sidecar.blocked_source_entries()

    sidecar.reset_cookie_store_tracking()

    assert sidecar.blocked_source_entries() == []
