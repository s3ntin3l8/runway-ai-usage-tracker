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


#: The GitHub CLI keeps one item per logged-in account plus an account-less copy
#: of the active account's token, which is what ``gh auth token`` returns
#: (macOS stores the empty account as no attribute; the helper reports it as "").
GH_SERVICE = "gh:github.com"


def select_item(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The one item a search result stands for, shared by every read and authorization path.

    Every path must pick the same item, or the user would authorize one item while the
    background read uses another. Only the GitHub CLI's account-less active-token item
    may win over its siblings; anything else with several matches stays ambiguous.
    """
    if not items:
        raise KeychainError("missing")
    if len(items) > 1 and all(item.get("service") == GH_SERVICE for item in items):
        # The ctypes backend omits the key instead of reporting "", so treat both alike.
        active = [item for item in items if item.get("account") in ("", None)]
        if len(active) == 1:
            return active[0]
    if len(items) != 1:
        raise KeychainError("ambiguous")
    return items[0]


def encode_frame(value: dict[str, Any]) -> bytes:
    data = plistlib.dumps(value, fmt=plistlib.FMT_BINARY)
    if not 0 < len(data) <= MAX_FRAME:
        raise KeychainError("unavailable")
    return struct.pack(">I", len(data)) + data
