"""The macOS Keychain entry the Antigravity CLI keeps its login in.

From agy 1.4.3 the CLI writes its OAuth token to the login Keychain (service
``gemini``, account ``antigravity``) and writes no token file at all.

This module holds what the sidecar needs to know about that entry - the
catalogued rule, the CLI version that decides whether the token file is still
the credential, and how to read the stored bundle. It deliberately imports
neither the consent map nor the Keychain worker: both lead back to the
sidecar (consent -> protocol -> settings -> setup -> sidecar), so a module
that imported them and was itself imported by the sidecar would close a new
import cycle. The reader for the stored value is therefore passed in.
"""

from __future__ import annotations

import platform
from collections.abc import Callable
from datetime import datetime
from typing import Any

SERVICE = "gemini"
ACCOUNT = "antigravity"
SOURCE_ID = "antigravity.agy-cli"
# agy moved its credential out of the token file in 1.4.3. The file an older
# install left behind is stale from then on, and treating it as the credential
# would respawn ``agy models`` every tick - the renewal writes the Keychain,
# never the file, so the file's expiry never advances. Only a CLI that still
# uses the file makes the file authoritative.
FILE_LAYOUT_LAST_VERSION = (1, 4, 3)


def rule() -> dict[str, Any] | None:
    """The catalogued rule for this entry, when there is one.

    It comes from the generated catalog, so it is the very rule the consent
    map was built from. Service *and* account are matched, so a second rule
    for the same service cannot be picked up by accident.
    """
    from scripts.sidecar_pkg import source_catalog

    providers = source_catalog.KEYCHAIN_RULES.get("antigravity")
    rules = providers.get("rules", []) if isinstance(providers, dict) else []
    for rule in rules:
        if (
            isinstance(rule, dict)
            and rule.get("type") == "keychain"
            and rule.get("service_name") == SERVICE
            and rule.get("account") == ACCOUNT
        ):
            return rule
    return None


def document(raw: bytes) -> dict[str, Any] | None:
    """The credential document in a stored value, or None when unreadable."""
    from scripts.sidecar_pkg.keyring_value import decode_document

    return decode_document(raw.decode("utf-8", "replace"))


def expiry_epoch(document: dict[str, Any]) -> float | None:
    """``token.expiry`` (ISO 8601) as epoch seconds; None when unreadable."""
    expiry = str((document.get("token") or {}).get("expiry") or "")
    if not expiry:
        return None
    try:
        return datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def uses_the_token_file() -> bool:
    """Whether the installed agy still keeps its credential in the token file.

    An unknown version answers False: a CLI that cannot report its version is
    no evidence that the file is live, and reading a stale file is exactly
    what makes the keep-alive respawn the renewal every tick.
    """
    from scripts.sidecar_pkg.agy_version import installed_version

    version = installed_version()
    if not version:
        return False
    try:
        parts = tuple(int(part) for part in version.split("."))
    except ValueError:
        return False
    return parts < FILE_LAYOUT_LAST_VERSION


def state(read_value: Callable[[], bytes | None]) -> tuple[bool, float | None]:
    """(present, expiry) of the agy login on this machine.

    ``read_value`` returns the stored value, or None when the entry is not
    available here (no consent, no entry). present=False means there is no
    login to renew; expiry=None with present=True means the credential is
    there but its expiry cannot be read, and the CLI decides.
    """
    if platform.system() != "Darwin":
        return False, None
    raw = read_value()
    if raw is not None:
        parsed = document(raw)
        return True, (None if parsed is None else expiry_epoch(parsed))
    # No Keychain entry (or no consent for it). The token file is the
    # credential only while the installed CLI still writes it.
    if not uses_the_token_file():
        return False, None
    from scripts.sidecar_pkg.keep_alive import DEFAULT_TOKEN_PATH
    from scripts.sidecar_pkg.keep_alive import expiry_epoch as file_expiry

    if not DEFAULT_TOKEN_PATH.is_file():
        return False, None
    return True, file_expiry(DEFAULT_TOKEN_PATH)
