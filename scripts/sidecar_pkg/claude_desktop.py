"""Read Claude Desktop's macOS session cookie for server-side quota collection.

No chat contents, quota-history percentages, or synthetic token events are read.
The server verifies the cookie's account identity before using it.
"""

import base64
import hashlib
import json
import logging
import math
import os
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

from scripts.sidecar_pkg.cookie_scan import StoreScan

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


def _oauth_cache(value: Any, key: bytes) -> dict[str, Any]:
    """Desktop safeStorage strings have no Chromium cookie host-hash prefix."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not isinstance(value, str):
        return {}
    blob = base64.b64decode(value, validate=True)
    if not blob.startswith(b"v10") or not blob[3:] or len(blob[3:]) % 16:
        raise ValueError("Unsupported Desktop OAuth encryption")
    decoder = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    padded = decoder.update(blob[3:]) + decoder.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    data = json.loads(unpadder.update(padded) + unpadder.finalize())
    if not isinstance(data, dict):
        raise ValueError("Unsupported Desktop OAuth cache")
    return data


def read_oauth_candidates(path: Path) -> list[tuple[dict[str, str], str]]:
    """Read only the two observed OAuth cache layouts; never export refresh secrets."""
    from scripts.sidecar_pkg import source_consent as consent

    ticket = consent.service_ticket("Claude Safe Storage")
    if ticket is None or ticket.source_id != "anthropic.desktop-oauth":
        return []
    if os.getenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "1").strip().lower() in {
        "0",
        "false",
        "off",
        "no",
    }:
        return []
    try:
        config = json.loads(path.read_text())
        if not isinstance(config, dict) or not any(
            config.get(name) for name in ("oauth:tokenCacheV2", "oauth:tokenCache")
        ):
            return []
    except (OSError, ValueError):
        return []
    key = _storage_key(path)
    if key is None:
        return []
    result: list[tuple[dict[str, str], str]] = []
    seen: set[str] = set()
    slots: set[str] = set()
    for cache_name in ("oauth:tokenCacheV2", "oauth:tokenCache"):
        try:
            entries = _oauth_cache(config.get(cache_name), key)
        except (ValueError, TypeError):
            _warn_once("unsupported OAuth cache")
            continue
        for slot, entry in entries.items():
            if not isinstance(slot, str) or slot in slots:
                continue
            # V2 tombstones and unsupported entries must not resurrect stale V1 logins.
            slots.add(slot)
            if not isinstance(entry, dict):
                continue
            token, expiry = entry.get("token"), entry.get("expiresAt")
            if not isinstance(token, str) or not token.strip():
                continue
            if (
                isinstance(expiry, bool)
                or not isinstance(expiry, int | float)
                or not math.isfinite(expiry)
            ):
                _warn_once("unsupported OAuth expiry")
                continue
            if not slot.startswith("acct:") or "|" not in slot:
                _warn_once("OAuth account binding unavailable")
                continue
            try:
                account_uuid = str(UUID(slot[5 : slot.index("|")]))
            except ValueError:
                _warn_once("unsupported OAuth account binding")
                continue
            if token in seen:
                continue
            seen.add(token)
            tokens = {
                "oauth_token": token,
                "expiry_date": str(int(expiry)),
                "oauth_account_uuid": account_uuid,
                "oauth_read_only": "1",
            }
            # Slot identity includes account, organization, client and scope, never the secret.
            origin = (
                "keychain:Claude Desktop/oauth/"
                + hashlib.pbkdf2_hmac("sha256", slot.encode(), b"runway-desktop-slot-v1", 1).hex()[
                    :24
                ]
            )
            result.append((tokens, origin))
    return result if consent.POLICY.current(ticket) else []


def scan_session_cookie(path: Path) -> StoreScan[str]:
    """Expose store-read evidence while sharing Desktop OAuth's cached key."""
    from scripts.sidecar_pkg.macos_cookies import scan_cookies

    return scan_cookies(
        path,
        "Claude Safe Storage",
        "claude.ai",
        "sessionKey",
        include_subdomains=False,
        prefixes=(b"v10",),
    )


def read_session_cookie(path: Path) -> str | None:
    """Compatibility wrapper: choose the first independently decoded live session."""
    values = scan_session_cookie(path).values
    return values[0] if values else None
