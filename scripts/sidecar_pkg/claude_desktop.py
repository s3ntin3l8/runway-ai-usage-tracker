"""Read Claude Desktop's macOS session cookie for server-side quota collection.

No chat contents, quota-history percentages, or synthetic token events are read.
The server verifies the cookie's account identity before using it.
"""

import hashlib
import hmac
import logging
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path

_KEY_CACHE: dict[str, tuple[float, bytes | None]] = {}
_KEY_LOCK = threading.Lock()
_WARNED: set[str] = set()


def _warn_once(reason: str) -> None:
    if reason not in _WARNED:
        _WARNED.add(reason)
        logging.warning("Claude Desktop credential unavailable: %s", reason)


def _storage_key(path: Path) -> bytes | None:
    """Cache success and refusal for ten minutes; never log Keychain output."""
    cache_id = str(path)
    with _KEY_LOCK:
        cached = _KEY_CACHE.get(cache_id)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        key = None
        failure_reason = "Keychain access failed (retry in ten minutes)"
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Safe Storage", "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                key = hashlib.pbkdf2_hmac(
                    "sha1", result.stdout.strip().encode(), b"saltysalt", 1003, 16
                )
        except (OSError, subprocess.SubprocessError):
            # A missing executable or timed-out prompt is a cached failure too.
            # Do not expose command output or exception text: either may contain secrets.
            failure_reason = "Keychain command did not complete (retry in ten minutes)"
        if key is None:
            _warn_once(failure_reason)
        _KEY_CACHE[cache_id] = (time.monotonic() + 600, key)
        return key


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
            key = _storage_key(path)
            if key is None:
                return None
            try:
                from cryptography.hazmat.primitives import padding
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

                decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
                padded = decryptor.update(encrypted[3:]) + decryptor.finalize()
                unpadder = padding.PKCS7(128).unpadder()
                plaintext = unpadder.update(padded) + unpadder.finalize()
                if version >= 24:
                    if not hmac.compare_digest(
                        plaintext[:32], hashlib.sha256(host.encode()).digest()
                    ):
                        _warn_once("cookie host binding failed")
                        continue
                    plaintext = plaintext[32:]
                value = plaintext.decode("utf-8")
                if value:
                    return value
            except ImportError:
                _warn_once("cookie decryption dependency unavailable")
                return None
            except (ValueError, UnicodeError):
                _warn_once("cookie decryption failed")
    except (OSError, sqlite3.Error, ValueError, TypeError):
        _warn_once("cookie database unreadable")
    return None
