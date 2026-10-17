"""Read Claude Desktop's macOS session cookie for server-side quota collection.

No chat contents, quota-history percentages, or synthetic token events are read.
The server verifies the cookie's account identity before using it.
"""

import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()


def _warn_once(reason: str) -> None:
    with _WARN_LOCK:
        if reason in _WARNED:
            return
        _WARNED.add(reason)
    logging.warning("Claude Desktop credential unavailable: %s", reason)


def _storage_key(path: Path) -> bytes | None:
    """Use the shared process cache; a restart is required after refusal."""
    from scripts.sidecar_pkg.macos_cookies import storage_key

    return storage_key("Claude Safe Storage")


def read_session_cookie(path: Path) -> str | None:
    """Read a non-expired sessionKey with strict padding and v24 host binding."""
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            version_row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            if version_row is None:
                _warn_once("cookie schema version missing")
                return None
            version = int(version_row[0])
            rows = conn.execute(
                "SELECT host_key, encrypted_value, expires_utc FROM cookies "
                "WHERE name = 'sessionKey' AND host_key IN ('claude.ai', '.claude.ai') "
                "ORDER BY expires_utc DESC"
            ).fetchall()
        for host, encrypted, expires in rows:
            # Chromium uses microseconds since 1601; zero is a session cookie.
            if expires and expires <= (time.time() + 11644473600) * 1_000_000:
                continue
            if not isinstance(encrypted, bytes) or not encrypted.startswith(b"v10"):
                _warn_once("unsupported cookie encryption")
                continue
            from scripts.sidecar_pkg.macos_cookies import decrypt

            value = decrypt(encrypted, "Claude Safe Storage", host, version)
            if value:
                return value
    except (OSError, sqlite3.Error, ValueError, TypeError):
        _warn_once("cookie database unreadable")
    return None
