"""Read-only, account-scoped Claude Code macOS Keychain discovery."""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import math
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg import macos_keychain_store as keychain
from scripts.sidecar_pkg.keychain_client import get_keychain_client

_WARNED: set[tuple[str, str]] = set()


def service_candidates(directories: list[Path]) -> list[str]:
    """Match Claude Code's production service derivation, including explicit overrides."""
    base = "Claude Code-credentials"
    override = os.getenv("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if override is not None:
        if not override:
            return [base]
        suffix = hashlib.sha256(unicodedata.normalize("NFC", override).encode()).hexdigest()[:8]
        return [f"{base}-{suffix}"]
    result = [base]
    for directory in directories:
        normalized = unicodedata.normalize("NFC", str(directory))
        suffix = hashlib.sha256(normalized.encode()).hexdigest()[:8]
        if f"{base}-{suffix}" not in result:
            result.append(f"{base}-{suffix}")
    return result


def _warn(service: str, reason: str) -> None:
    if (service, reason) not in _WARNED:
        _WARNED.add((service, reason))
        logging.warning("Claude Code Keychain: %s", reason)


def _mapped_value(data: dict[str, Any], field: str) -> Any:
    """Only the declared dot paths and identity alternatives used by this layout."""
    for alternative in field.split("|"):
        value: Any = data
        for segment in alternative.split("."):
            value = value.get(segment) if isinstance(value, dict) else None
        if value is not None:
            return value
    return None


def account_name() -> str:
    try:
        account = os.getenv("USER") or getpass.getuser()
    except (OSError, KeyError):
        account = "claude-code-user"
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", account):
        account = "claude-code-user"
    return account


def access_blocked() -> bool:
    """Discovery and renewal share a process-wide denial/timeout circuit breaker."""
    return get_keychain_client().access_blocked("claude-code")


def block_access() -> None:
    get_keychain_client().block_access("claude-code")


def read_candidates(
    directories: list[Path], mapping: dict[str, str]
) -> list[tuple[dict[str, Any], str]]:
    from scripts.sidecar_pkg import source_consent as consent

    ticket = consent.POLICY.ticket("anthropic.claude-code")
    if ticket is None or access_blocked():
        return []
    account = account_name()
    candidates = []
    for service in service_candidates(directories):
        try:
            raw = keychain.read_secret(service, account, group="claude-code")
        except keychain.KeychainError as exc:
            if exc.reason == "cancelled":
                break
            block_access()
            _warn(
                service,
                "unavailable or timed out; restart sidecar to retry"
                if exc.reason == "timed_out"
                else "access unavailable; restart sidecar to retry",
            )
            break
        if raw is None:
            _warn(service, "item missing")
            continue
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Unsupported layout")
            tokens = {
                target: value
                for field, target in mapping.items()
                if (value := _mapped_value(data, field)) is not None
            }
            if not isinstance(tokens.get("oauth_token"), str) or not tokens["oauth_token"]:
                _warn(service, "access token missing")
                continue
            tokens.pop("refresh_token", None)
            expiry = tokens.get("expiry_date")
            if expiry is not None:
                if (
                    isinstance(expiry, bool)
                    or not isinstance(expiry, int | float)
                    or not math.isfinite(expiry)
                ):
                    _warn(service, "unsupported expiry")
                    continue
                tokens["expiry_date"] = str(int(expiry))
                if expiry / 1000 <= time.time():
                    _warn(service, "login expired; renew in Claude Code")
            tokens["oauth_read_only"] = "1"
            candidates.append((tokens, f"keychain:{service}"))
        except (ValueError, TypeError, AttributeError):
            _warn(service, "unsupported JSON layout")
    return candidates if consent.POLICY.current(ticket) else []
