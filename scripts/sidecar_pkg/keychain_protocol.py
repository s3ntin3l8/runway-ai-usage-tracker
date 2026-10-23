"""Secret-free errors and bounded private Keychain framing shared by both endpoints."""

from __future__ import annotations

import plistlib
import struct
from typing import Any

MAX_FRAME = 4 * 1024 * 1024
TIMEOUT = 30
OPERATIONS = frozenset({"copy", "check_update", "update", "add", "delete"})


class KeychainError(Exception):
    """A sanitized Keychain failure, safe to log without native diagnostic output."""

    def __init__(self, reason: str, *, native_status: int | None = None) -> None:
        self.reason = reason
        self.native_status = native_status
        super().__init__(reason)


#: The reasons a KeychainError may carry that are safe to show a user. A
#: reason outside this set is replaced with "unavailable", so nothing about
#: the keychain can reach the output.
KEYCHAIN_REASONS = frozenset(
    {"missing", "ambiguous", "denied", "unavailable", "identity changed", "timed_out"}
)


def keychain_reason(exc: KeychainError) -> str:
    """The reason to show a user, with the native status when there is one.

    Both authorization entrypoints use this, so a denied read reads the same
    whichever path refused it.
    """
    reason = exc.reason if exc.reason in KEYCHAIN_REASONS else "unavailable"
    status = f" (OSStatus {exc.native_status})" if type(exc.native_status) is int else ""
    return f"{reason}{status}"


def encode_frame(value: dict[str, Any]) -> bytes:
    data = plistlib.dumps(value, fmt=plistlib.FMT_BINARY)
    if not 0 < len(data) <= MAX_FRAME:
        raise KeychainError("unavailable")
    return struct.pack(">I", len(data)) + data
