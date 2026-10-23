"""Classify a browser cookie store before reading it.

A cookie store can fail to open for several unrelated reasons, and the
sidecar used to report every one of them as the same
``sqlite3.OperationalError`` — once per cookie name, for every store, on
every cycle. That produced thousands of tracebacks, evicted the log's
rotation budget, and made "the browser is open" indistinguishable from "the
value format is unsupported".

This module turns a store into a classified state, so the caller probes it
once per cycle, skips it with a reason, and can tell the operator what to
do about it.

Two locks hide behind the same Python-level symptom, and they are not the
same problem:

* Chrome 127+ on Windows opens the active profile's ``Cookies`` store with a
  Win32 sharing mode that denies reads (``ERROR_SHARING_VIOLATION``). The
  file cannot be copied either, so no process outside the browser can read
  it while the browser runs. Its values are additionally App-Bound Encrypted
  (``v20``), which no user-context DPAPI call can unwrap.
* Firefox holds ``cookies.sqlite`` with a SQLite-level lock. The file itself
  stays shareable, so copying it aside and opening the copy works while
  Firefox is running.

Only the second is recoverable here, and it is the difference between
Firefox cookie collection working on Windows and not working at all.
"""

import ctypes
import logging
import shutil
import sqlite3
import sys
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Win32 error codes that separate "held by another process" from "denied".
_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33

_GENERIC_READ = 0x80000000
_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80

# Chrome 127+ writes cookie values with App-Bound Encryption, tagged v20.
# v10/v11 remain user-DPAPI decryptable.
_APP_BOUND_PREFIX = b"v20"

_FORMAT_DETAIL = "values are app-bound encrypted (v20) and need a browser-side read"


class StoreState(StrEnum):
    """Why a cookie store can or cannot be read."""

    READABLE = "readable"
    LOCKED = "locked"
    MISSING = "missing"
    NOT_SQLITE = "not_sqlite"
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNREADABLE = "unreadable"


# One sentence per unusable state. This is the fixed vocabulary the report to
# the server uses: a raw sqlite message can embed a filesystem path, and the
# report must carry no host detail.
STATE_DETAILS: dict[StoreState, str] = {
    StoreState.LOCKED: "held open by another process",
    StoreState.UNSUPPORTED_FORMAT: _FORMAT_DETAIL,
    StoreState.UNREADABLE: "the file could not be opened",
    StoreState.NOT_SQLITE: "the file is not a SQLite database",
}

# Why the server should call a store unavailable, per state. The server
# mirrors this vocabulary (``app/services/blocked_sources.py``); a parity
# test keeps the two equal.
STATE_REASONS: dict[StoreState, str] = {
    StoreState.LOCKED: "source_locked",
    StoreState.UNSUPPORTED_FORMAT: "source_unsupported",
    StoreState.UNREADABLE: "source_unreadable",
    StoreState.NOT_SQLITE: "source_unreadable",
}

# The same vocabulary as a set, for the places that ask "is this report about a
# store the sidecar could not read?". The egress filter needs it: a report
# about an unusable store carries no keychain-derived material, so it is not
# gated by the Keychain consent rules the way a cookie origin is.
SOURCE_BLOCK_REASONS = frozenset(STATE_REASONS.values())

# The keys a source report carries, and nothing else: the provider, the origin
# it names, the store, the reason and the remedy. No secret, no account id, no
# path. The egress filter passes a report through on this shape alone, so
# "carries no keychain-derived material" is a property of the report rather
# than a promise about whoever built it.
SOURCE_REPORT_KEYS = frozenset(
    {
        "provider_id",
        "credential_origin",
        "reason",
        "source_store",
        "source_detail",
        "source_remedy",
    }
)


@dataclass(frozen=True)
class StoreProbe:
    """The classified state of one cookie store.

    ``read_path`` is where the store can actually be read, when that differs
    from ``path`` — a SQLite-level lock is sidestepped by reading a copy.
    """

    path: Path
    browser: str
    profile: str
    store: str
    state: StoreState
    detail: str = ""
    read_path: Path | None = None

    @property
    def readable(self) -> bool:
        """Whether the store file can be opened.

        An app-bound store is readable as a file but its values cannot be
        decrypted, so it is still worth opening: a store that mixes v10 and
        v20 values keeps its decryptable ones.
        """
        return self.state in (StoreState.READABLE, StoreState.UNSUPPORTED_FORMAT)

    def identity(self) -> str:
        """Name the store for a log line, e.g. ``Chrome/Default/Network/Cookies``."""
        return "/".join(part for part in (self.browser, self.profile, self.store) if part)


def is_app_bound(value: object) -> bool:
    """Whether a stored value is app-bound encrypted (Chrome 127+ v20).

    A store can hold both kinds: Chrome 127+ keeps the cookies written
    before the upgrade as v10 and writes v20 only for the ones set
    afterwards. The value in front of the caller is therefore better
    evidence than a sample of the table.
    """
    return isinstance(value, (bytes, bytearray)) and bytes(value).startswith(_APP_BOUND_PREFIX)


def mark_app_bound(probe: StoreProbe) -> StoreProbe:
    """Reclassify a probed store as app-bound once a value proves it.

    The probe samples one row, so a store whose first row is a pre-upgrade
    v10 value is called readable even when the provider's own cookie is
    app-bound. The reclassified probe replaces the cached one, so the store
    is reported as unusable from then on.
    """
    upgraded = replace(
        probe,
        state=StoreState.UNSUPPORTED_FORMAT,
        detail=STATE_DETAILS[StoreState.UNSUPPORTED_FORMAT],
    )
    with _cache_lock:
        for key, cached in _probe_cache.items():
            if cached.identity() == probe.identity():
                _probe_cache[key] = upgraded
                break
    return upgraded


_probe_cache: dict[tuple[str, str], StoreProbe] = {}
_copy_dirs: list[Path] = []
_cache_lock = threading.Lock()


def reset_store_probe_cache() -> None:
    """Forget every cached probe and delete the copies made for this cycle.

    Called at the start of a collection cycle so a store that Chrome locked
    five minutes ago is re-examined, and so the sidecar re-reads a store
    the operator just unlocked.
    """
    global _copy_dirs
    with _cache_lock:
        _probe_cache.clear()
        stale, _copy_dirs = _copy_dirs, []
    for directory in stale:
        shutil.rmtree(directory, ignore_errors=True)


def probe_store(target: Mapping[str, Any]) -> StoreProbe:
    """Classify one cookie store, reusing this cycle's verdict for it.

    ``target`` is one entry of :meth:`BrowserCookieExtractor.get_all_paths`.
    """
    path = Path(target["path"])
    browser = str(target.get("browser") or target.get("type") or "browser")
    profile = str(target.get("profile") or _derive_profile(path))
    store = str(target.get("store") or path.name)
    kind = str(target.get("type") or "")

    key = (str(path), kind)
    with _cache_lock:
        cached = _probe_cache.get(key)
    if cached is not None:
        return cached

    probe = _classify(path, browser, profile, store, kind)
    with _cache_lock:
        _probe_cache[key] = probe
    return probe


def _derive_profile(path: Path) -> str:
    """Guess the profile a store belongs to when the caller did not say.

    Chromium nests the store under ``<profile>/Network/Cookies``; Firefox
    puts it straight in ``<profile>/cookies.sqlite``.
    """
    parent = path.parent
    if parent.name.lower() == "network":
        return parent.parent.name
    return parent.name


def _classify(path: Path, browser: str, profile: str, store: str, kind: str) -> StoreProbe:
    """Open the store once and record what happened."""
    if not path.exists():
        return StoreProbe(path, browser, profile, store, StoreState.MISSING)

    connection, state, detail, code = _open_ro(path)
    if connection is not None:
        try:
            format_state = _value_format(connection, kind)
            return StoreProbe(
                path,
                browser,
                profile,
                store,
                format_state,
                "" if format_state is StoreState.READABLE else _FORMAT_DETAIL,
            )
        finally:
            connection.close()

    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        # A SQLite-level lock: the file itself is shareable, so read a copy.
        copy = _copy_store(path)
        if copy is not None:
            copied, copy_state, copy_detail, _ = _open_ro(copy)
            if copied is not None:
                try:
                    format_state = _value_format(copied, kind)
                    return StoreProbe(
                        path,
                        browser,
                        profile,
                        store,
                        format_state,
                        "" if format_state is StoreState.READABLE else _FORMAT_DETAIL,
                        read_path=copy,
                    )
                finally:
                    copied.close()
            detail = copy_detail or detail
            state = copy_state

        # The store was locked; if the copy could not be read either, the
        # reason it failed is the one worth reporting.
        return StoreProbe(path, browser, profile, store, state, detail)

    return StoreProbe(path, browser, profile, store, state, detail)


def _open_ro(
    path: Path, timeout: float = 0.0
) -> tuple[sqlite3.Connection | None, StoreState, str, int | None]:
    """Open a store read-only and force the file open.

    ``sqlite3.connect`` is lazy: a missing or unreadable file only raises on
    the first statement, so a trivial query is issued to find out what is
    really wrong. The sqlite error code is returned alongside the state
    because a SQLite-level lock (recoverable by copying) and a Win32 sharing
    violation (not recoverable) are both reported as "locked".

    The default busy timeout is zero: a store another process holds should
    be reported straight away and copied aside, not waited on. Waiting for
    the lock is the caller's choice, and the caller that wants it is
    :func:`open_store`.
    """
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.OperationalError as exc:
        # A read-only open of an unreachable file fails here rather than on
        # the first statement, so it takes the same classification path.
        code = _primary_code(exc)
        state, detail = _operational_state(path, exc, code)
        return None, state, detail, code
    except sqlite3.Error as exc:
        return None, StoreState.UNREADABLE, str(exc), _primary_code(exc)
    try:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.OperationalError as exc:
        connection.close()
        code = _primary_code(exc)
        state, detail = _operational_state(path, exc, code)
        return None, state, detail, code
    except sqlite3.DatabaseError as exc:
        connection.close()
        return None, StoreState.NOT_SQLITE, str(exc), _primary_code(exc)
    return connection, StoreState.READABLE, "", None


def _primary_code(exc: sqlite3.Error) -> int | None:
    """The primary sqlite result code, without the extended-code bits.

    ``sqlite_errorcode`` reports the extended code — "unable to open database
    file" arrives as 526, not 14 — so it has to be masked before it can be
    compared with ``SQLITE_CANTOPEN`` and friends.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return None
    return code & 0xFF


def _operational_state(
    path: Path, exc: sqlite3.OperationalError, code: int | None
) -> tuple[StoreState, str]:
    """Map a sqlite open failure onto a store state and a reason."""
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return StoreState.LOCKED, str(exc)
    if code == sqlite3.SQLITE_CANTOPEN:
        win_state, win_detail = _win32_access_state(path)
        if win_state is not None:
            return win_state, win_detail
        return StoreState.UNREADABLE, str(exc)
    if code == sqlite3.SQLITE_NOTADB:
        return StoreState.NOT_SQLITE, str(exc)
    return StoreState.UNREADABLE, str(exc)


def _value_format(connection: sqlite3.Connection, kind: str) -> StoreState:
    """Detect a value format the sidecar cannot decrypt.

    Chromium stores app-bound-encrypted values in ``encrypted_value``; the
    v10/v11 schemes stay user-DPAPI decryptable. Firefox stores plaintext in
    ``moz_cookies.value`` and is never app-bound.
    """
    if kind != "chromium":
        return StoreState.READABLE
    try:
        row = connection.execute("SELECT encrypted_value FROM cookies LIMIT 1").fetchone()
    except sqlite3.Error:
        # An unfamiliar schema is not evidence of an unsupported format.
        return StoreState.READABLE
    if row and is_app_bound(row[0]):
        return StoreState.UNSUPPORTED_FORMAT
    return StoreState.READABLE


def _copy_store(path: Path) -> Path | None:
    """Copy a shareable-but-locked store aside so it can be read.

    The copy is best-effort: a browser mid-transaction can produce a torn
    snapshot, and the caller must tolerate that rather than trust it.
    """
    try:
        directory = Path(tempfile.mkdtemp(prefix="runway-cookie-store-"))
        copy = directory / path.name
        shutil.copyfile(path, copy)
        # Carry the write-ahead log and rollback journal so the copy is
        # consistent rather than a snapshot taken mid-transaction.
        for suffix in ("-wal", "-journal"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                shutil.copyfile(sidecar, copy.with_name(copy.name + suffix))
    except OSError:
        logger.debug("Cookie store copy failed", exc_info=True)
        return None
    with _cache_lock:
        _copy_dirs.append(directory)
    return copy


def _win32_access_state(path: Path) -> tuple[StoreState | None, str]:
    """Classify a file's accessibility using the raw Win32 error code.

    Returns ``(None, "")`` on platforms that cannot answer, or when the file
    turns out to be readable — there is nothing to report in that case.
    """
    if sys.platform != "win32":
        return None, ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        # The default c_int restype truncates the 64-bit handle and reports
        # a failed open as a successful one.
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    except (AttributeError, OSError):
        return None, ""

    handle = kernel32.CreateFileW(
        str(path),
        _GENERIC_READ,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        if code in (_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION):
            return StoreState.LOCKED, "held open by another process"
        if code == _ERROR_ACCESS_DENIED:
            return StoreState.UNREADABLE, "access denied"
        if code == _ERROR_FILE_NOT_FOUND:
            return StoreState.MISSING, "not found"
        return StoreState.UNREADABLE, f"Win32 error {code}"
    kernel32.CloseHandle(handle)
    return None, ""


def open_store(probe: StoreProbe) -> sqlite3.Connection | None:
    """Open a classified store for reading, or return None.

    Unlike the probe this waits for a lock rather than copying around it:
    the caller wants the value, and a store that is only briefly busy should
    still be readable.
    """
    if not probe.readable:
        return None
    target = probe.read_path or probe.path
    connection, _state, _detail, _code = _open_ro(target, timeout=5.0)
    return connection


def probed_stores() -> int:
    """How many stores this cycle has classified."""
    with _cache_lock:
        return len(_probe_cache)


def unusable_stores() -> list[StoreProbe]:
    """The stores probed this cycle that could not be used.

    A missing store is not listed: a browser that has one profile simply
    has no store for the others, and that is not a problem to report.
    """
    with _cache_lock:
        probes = list(_probe_cache.values())
    return [
        probe
        for probe in probes
        if probe.state
        in (
            StoreState.LOCKED,
            StoreState.UNSUPPORTED_FORMAT,
            StoreState.NOT_SQLITE,
            StoreState.UNREADABLE,
        )
    ]
