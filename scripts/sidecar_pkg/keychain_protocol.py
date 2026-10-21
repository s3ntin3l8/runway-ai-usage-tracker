"""Secret-free errors and bounded private Keychain framing shared by both endpoints."""

from __future__ import annotations

import plistlib
import struct
from typing import Any

MAX_FRAME = 4 * 1024 * 1024
TIMEOUT = 30
OPERATIONS = frozenset({"copy", "update", "add", "delete"})


class KeychainError(Exception):
    """A sanitized Keychain failure, safe to log without native diagnostic output."""

    def __init__(self, reason: str, *, native_status: int | None = None) -> None:
        self.reason = reason
        self.native_status = native_status
        super().__init__(reason)


def encode_frame(value: dict[str, Any]) -> bytes:
    data = plistlib.dumps(value, fmt=plistlib.FMT_BINARY)
    if not 0 < len(data) <= MAX_FRAME:
        raise KeychainError("unavailable")
    return struct.pack(">I", len(data)) + data
