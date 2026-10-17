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

_KEY_CACHE: dict[str, bytes | None] = {}
_KEY_LOCK = threading.Lock()


def storage_key(service: str) -> bytes | None:
    with _KEY_LOCK:
        if service in _KEY_CACHE:
            return _KEY_CACHE[service]
        key = None
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                key = hashlib.pbkdf2_hmac(
                    "sha1", result.stdout.strip().encode(), b"saltysalt", 1003, 16
                )
        except (OSError, subprocess.SubprocessError):
            pass
        if key is None:
            logging.warning("Cookie Keychain access unavailable; restart sidecar to retry")
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
        pass
    names.update(
        p.name
        for p in base.iterdir()
        if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile "))
    )
    return [
        base / name
        for name in sorted(names)
        if isinstance(name, str)
        and name not in {".", ".."}
        and Path(name).name == name
        and (base / name).is_dir()
    ]


def read_cookies(path: Path, service: str, domain: str, name: str) -> list[str]:
    values = []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            if row is None:
                return []
            version = int(row[0])
            rows = conn.execute(
                "SELECT host_key, encrypted_value, expires_utc FROM cookies WHERE name = ? AND (host_key = ? OR host_key = ? OR host_key LIKE ?)",
                (name, domain, "." + domain, "%." + domain),
            ).fetchall()
        for host, blob, expires in rows:
            if expires and expires <= (time.time() + 11644473600) * 1_000_000:
                continue
            value = decrypt(blob, service, host, version)
            if value and value not in values:
                values.append(value)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        pass
    return values
