"""macOS Chromium cookie decoding. Keys and failed lookups live only in memory."""

import hashlib
import hmac
import json
import logging
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path

from scripts.sidecar_pkg.cookie_scan import ScanStatus, StoreScan

_KEY_CACHE: dict[str, bytes | None] = {}
_KEY_LOCK = threading.Lock()
_KEY_STATUS: dict[str, ScanStatus] = {}


def storage_key(service: str) -> bytes | None:
    with _KEY_LOCK:
        if service in _KEY_CACHE:
            return _KEY_CACHE[service]
        key = None
        status: ScanStatus = "inaccessible"
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True,
                text=True,
                timeout=30 if service == "Claude Safe Storage" else 5,
            )
            status = {44: "missing", 128: "denied", 51: "denied"}.get(
                result.returncode, "inaccessible"
            )
            if result.returncode == 0 and result.stdout.strip():
                key = hashlib.pbkdf2_hmac(
                    "sha1", result.stdout.strip().encode(), b"saltysalt", 1003, 16
                )
        except subprocess.TimeoutExpired:
            status = "timed_out"
        except (OSError, subprocess.SubprocessError):
            # Fall through to cache the failure and suppress further prompts until restart.
            pass
        if key is None:
            logging.warning("Cookie Keychain access unavailable; restart sidecar to retry")
        _KEY_STATUS[service] = "candidates" if key is not None else status
        _KEY_CACHE[service] = key
        return key


def decrypt(blob: bytes, service: str, host: str | None = None, version: int = 23) -> str | None:
    if (
        not isinstance(blob, bytes)
        or not blob.startswith((b"v10", b"v11"))
        or not blob[3:]
        or len(blob[3:]) % 16
    ):
        return None
    if version >= 24 and not host:
        return None
    key = storage_key(service)
    if key is None:
        return None
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        decoder = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        padded = decoder.update(blob[3:]) + decoder.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        raw = unpadder.update(padded) + unpadder.finalize()
        if version >= 24:
            if not hmac.compare_digest(raw[:32], hashlib.sha256(str(host).encode()).digest()):
                return None
            raw = raw[32:]
        value = raw.decode("utf-8")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return None
        return value or None
    except (ImportError, ValueError, UnicodeError):
        return None


def profiles(base: Path) -> list[Path]:
    names = set()
    try:
        state = json.loads((base / "Local State").read_text())
        names.update(state.get("profile", {}).get("info_cache", {}))
    except (OSError, ValueError, TypeError, AttributeError):
        # Missing or malformed Local State still permits filesystem profile discovery below.
        pass
    try:
        entries = list(base.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        try:
            if entry.is_dir() and (entry.name == "Default" or entry.name.startswith("Profile ")):
                names.add(entry.name)
        except OSError:
            continue
    result = []
    for name in sorted(names):
        if not isinstance(name, str) or name in {".", ".."} or Path(name).name != name:
            continue
        try:
            if (base / name).is_dir():
                result.append(base / name)
        except OSError:
            continue
    return result


def scan_cookies(
    path: Path,
    service: str,
    domain: str,
    name: str,
    *,
    include_subdomains: bool = True,
    prefixes: tuple[bytes, ...] = (b"v10", b"v11"),
) -> StoreScan[str]:
    values: list[str] = []
    expired = rejected = 0
    try:
        path.stat()
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        ) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            if row is None or not 1 <= int(row[0]) <= 24:
                return StoreScan("unsupported", schema_version=int(row[0]) if row else None)
            version = int(row[0])
            host_clause = "(host_key = ? OR host_key = ?)"
            params = [name, domain, "." + domain]
            if include_subdomains:
                host_clause = "(host_key = ? OR host_key = ? OR host_key LIKE ?)"
                params.append("%." + domain)
            rows = conn.execute(
                "SELECT host_key, encrypted_value, expires_utc FROM cookies WHERE name = ? AND "
                + host_clause
                + " ORDER BY expires_utc DESC",
                params,
            ).fetchall()
        failures: set[ScanStatus] = set()
        for host, blob, expires in rows:
            if expires and expires <= (time.time() + 11644473600) * 1_000_000:
                expired += 1
                continue
            if not isinstance(blob, bytes) or not blob.startswith(prefixes):
                rejected += 1
                failures.add("unsupported")
                continue
            value = decrypt(blob, service, host, version)
            if value:
                if value not in values:
                    values.append(value)
            else:
                rejected += 1
                failures.add(
                    _KEY_STATUS.get(service, "inaccessible")
                    if service in _KEY_CACHE and _KEY_CACHE[service] is None
                    else "unsupported"
                )
        failure = next(
            (
                status
                for status in ("timed_out", "denied", "inaccessible", "missing", "unsupported")
                if status in failures
            ),
            None,
        )
        # Partial decoding must not be mistaken for a successful completed scan.
        return StoreScan(
            failure or ("candidates" if values else "empty"), values, expired, rejected, version
        )
    except FileNotFoundError:
        return StoreScan("missing")
    except sqlite3.OperationalError as exc:
        # SQLite reports unsupported tables/columns with SQLITE_ERROR; access and
        # locking failures have distinct native codes. Never log raw SQL errors.
        code = getattr(exc, "sqlite_errorcode", None)
        return StoreScan("unsupported" if code == sqlite3.SQLITE_ERROR else "inaccessible")
    except PermissionError:
        return StoreScan("inaccessible")
    except OSError:
        return StoreScan("inaccessible")
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        return StoreScan("unsupported")


def read_cookies(path: Path, service: str, domain: str, name: str) -> list[str]:
    """Compatibility wrapper; extraction callers retain their list-returning API."""
    return scan_cookies(path, service, domain, name).values
