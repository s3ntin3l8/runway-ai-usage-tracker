"""Locally opted-in Claude Code Keychain renewal with durable recovery.

An intent with no durable response is ambiguous and must never be exchanged again.
The provider may have spent the token even if the network request timed out.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.anthropic_renewer import (
    FALLBACK_EXPIRES_IN,
    LEAD_SECONDS,
    RefreshRejectedError,
    request_refresh,
)
from scripts.sidecar_pkg.claude_code_keychain import (
    access_blocked,
    account_name,
    block_access,
    service_candidates,
)
from scripts.sidecar_pkg.claude_renewal_lock import claude_renewal_lock
from scripts.sidecar_pkg.macos_keychain_store import KeychainError, KeychainItem, KeychainStore

logger = logging.getLogger(__name__)


def _oauth(item: KeychainItem) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        data = json.loads(item.data)
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(oauth, dict):
            raise ValueError
        return data, oauth
    except (ValueError, TypeError):
        raise KeychainError("unsupported login") from None


def _replacement(item: KeychainItem, response: dict[str, Any]) -> bytes:
    data, oauth = _oauth(item)
    access, refresh = response.get("access_token"), response.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise KeychainError("unsupported refresh response")
    oauth["accessToken"], oauth["refreshToken"] = access, refresh
    expires = response.get("expires_in")
    if (
        isinstance(expires, bool)
        or not isinstance(expires, int | float)
        or not math.isfinite(expires)
        or expires <= 0
    ):
        expires = FALLBACK_EXPIRES_IN
    now = time.time()
    oauth["expiresAt"] = int((now + expires) * 1000)
    refresh_expiry = response.get("refresh_token_expires_in")
    if (
        not isinstance(refresh_expiry, bool)
        and isinstance(refresh_expiry, int | float)
        and math.isfinite(refresh_expiry)
        and refresh_expiry > 0
    ):
        oauth["refreshTokenExpiresAt"] = int((now + refresh_expiry) * 1000)
    scope = response.get("scope")
    if isinstance(scope, str) and scope.split():
        oauth["scopes"] = scope.split()
    return json.dumps(data).encode()


def _read_target(store: KeychainStore, expected: KeychainItem) -> KeychainItem:
    current = store.read(expected.reference)
    if current.service != expected.service or current.account != expected.account:
        raise KeychainError("identity changed")
    return current


class KeychainRenewer:
    """A Claude renewer composed with the existing file renewer under one provider."""

    name = "anthropic"

    def __init__(
        self,
        directories: Callable[[], list[Path]],
        enabled: Callable[[], bool],
        store_factory: Callable[[], KeychainStore] | None = None,
    ) -> None:
        self._directories = directories
        self._enabled = enabled
        self._store_factory = store_factory or (lambda: KeychainStore(allowed=self._allowed))
        self._last_outcome: dict[bytes | str, str] = {}
        # A response which could not be journaled is retried as a SAVE, never an exchange.
        self._pending: dict[bytes, dict[str, Any]] = {}

    def _allowed(self) -> bool:
        return sys.platform == "darwin" and self._enabled() is True and not access_blocked()

    def _log(self, target: bytes | str, outcome: str) -> None:
        if self._last_outcome.get(target) != outcome:
            self._last_outcome[target] = outcome
            logger.info("Claude Code Keychain keep-alive: %s", outcome)

    def due(self) -> bool:
        if not self._allowed():
            self._log("permission", "local permission disabled or unavailable")
            return False
        return True  # Recovery must run even when the current login is not due.

    def _require_permission(self) -> None:
        if not self._allowed():
            raise KeychainError("permission disabled")

    def renew(self) -> bool:
        if not self.due():
            return True
        with claude_renewal_lock() as acquired:
            if not acquired:
                return False
            ok = True
            store = self._store_factory()
            for service in service_candidates(self._directories()):
                if not self._allowed():
                    break
                try:
                    item = store.find(service, account_name())
                    if item is not None:
                        ok = self._renew_item(store, item) and ok
                except KeychainError as exc:
                    if exc.reason in {"denied", "timed_out", "unavailable"}:
                        block_access()
                    self._log(service, "permission unavailable" if access_blocked() else exc.reason)
                    ok = False
            return ok

    def _clear(self, store: KeychainStore, reference: bytes) -> None:
        self._require_permission()
        store.delete_journal(reference)
        self._pending.pop(reference, None)

    def _save(self, store: KeychainStore, reference: bytes, record: dict[str, Any]) -> None:
        self._require_permission()
        store.save_journal(reference, record)

    def _renew_item(self, store: KeychainStore, item: KeychainItem) -> bool:
        ref = item.reference
        record = self._pending.get(ref) or store.read_journal(ref)
        _, oauth = _oauth(item)
        refresh = oauth.get("refreshToken")
        if record is not None:
            if record.get("phase") not in {"intent", "pending", "rejected"} or not isinstance(
                record.get("refresh"), str
            ):
                raise KeychainError("invalid recovery record")
            if record["phase"] == "pending":
                return self._finish(store, item, record)
            if refresh == record["refresh"]:
                self._log(
                    ref,
                    "refresh rejected"
                    if record["phase"] == "rejected"
                    else "recovery pending; renew or log in with Claude Code",
                )
                return False
            self._clear(store, ref)
            self._log(ref, "superseded")
        if not isinstance(refresh, str) or not refresh:
            self._log(ref, "refresh token unavailable")
            return True
        expiry = oauth.get("expiresAt")
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, int | float)
            or not math.isfinite(expiry)
        ):
            self._log(ref, "unsupported expiry; renew in Claude Code")
            return True
        if expiry / 1000 > time.time() + LEAD_SECONDS:
            self._log(ref, "not due")
            return True
        # Re-read immediately before intent/exchange; never spend a stale snapshot.
        current = _read_target(store, item)
        if _oauth(current)[1].get("refreshToken") != refresh:
            self._log(ref, "superseded")
            return True
        intent = {"phase": "intent", "refresh": refresh}
        self._save(store, ref, intent)
        current = _read_target(store, item)
        if _oauth(current)[1].get("refreshToken") != refresh:
            self._clear(store, ref)
            self._log(ref, "superseded")
            return True
        self._require_permission()
        scopes = oauth.get("scopes")
        try:
            response = request_refresh(
                refresh,
                tuple(s for s in scopes if isinstance(s, str)) if isinstance(scopes, list) else (),
            )
        except RefreshRejectedError:
            self._save(store, ref, {"phase": "rejected", "refresh": refresh})
            self._log(ref, "refresh rejected")
            return False
        except OSError:
            self._log(ref, "recovery pending; exchange outcome unknown")
            return False
        # Store only the replacement JSON, never arbitrary provider response bodies.
        record = {
            "phase": "pending",
            "refresh": refresh,
            "replacement": _replacement(current, response),
        }
        self._pending[ref] = record
        return self._finish(store, current, record)

    def _finish(self, store: KeychainStore, item: KeychainItem, record: dict[str, Any]) -> bool:
        ref = item.reference
        replacement = record.get("replacement")
        if not isinstance(replacement, bytes):
            raise KeychainError("invalid recovery record")
        replacement_item = KeychainItem(ref, replacement, item.service, item.account)
        replacement_oauth = _oauth(replacement_item)[1]
        if not isinstance(replacement_oauth.get("refreshToken"), str):
            raise KeychainError("invalid recovery record")
        # Persist before consulting the target, including when it was deleted mid-exchange.
        self._save(store, ref, record)
        current = _read_target(store, item)
        data, oauth = _oauth(current)
        if oauth == replacement_oauth:
            # A previous write succeeded; verification/cleanup was interrupted.
            self._clear(store, ref)
            return True
        if oauth.get("refreshToken") != record["refresh"]:
            self._clear(store, ref)
            self._log(ref, "superseded")
            return True
        # Preserve unrelated JSON changes made while the HTTP request was in flight.
        current = _read_target(store, item)
        data, oauth = _oauth(current)
        if oauth.get("refreshToken") != record["refresh"]:
            self._clear(store, ref)
            self._log(ref, "superseded")
            return True
        for key in ("accessToken", "refreshToken", "expiresAt", "refreshTokenExpiresAt", "scopes"):
            if key in replacement_oauth:
                oauth[key] = replacement_oauth[key]
        data["claudeAiOauth"] = oauth
        self._require_permission()
        store.update(ref, json.dumps(data).encode())
        verified = _oauth(_read_target(store, item))[1]
        if verified != oauth:
            self._log(ref, "recovery pending; write verification failed")
            return False
        self._clear(store, ref)
        self._log(ref, "renewed")
        return True
