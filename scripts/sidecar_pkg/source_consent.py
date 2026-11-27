"""Local Keychain consent. Fleet can narrow permission, never grant it."""

from __future__ import annotations

import copy
import ctypes
import inspect
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.source_catalog import catalog, trusted_rule

READ_STATE_MAX_AGE = 7 * 86400
REAUTH_REQUIRED_STATE = "Needs re-authorization (app updated)"
_REQUIRED_RECORD_KEYS = {
    "revision",
    "last_read_at",
    "last_block_reason",
    "last_block_at",
}
_IDENTITY_KEYS = ("helper_identity", "notified_helper_identity")
# Before the helper was the identity, these held the main app's cdhash, which changes on
# every release. They are still accepted on load (old state files stay valid) but dropped.
_LEGACY_IDENTITY_KEYS = ("code_identity", "notified_code_identity")
_OPTIONAL_RECORD_KEYS = set(_IDENTITY_KEYS) | set(_LEGACY_IDENTITY_KEYS)
_BLOCK_REASONS = {"denied", "timed_out", "unavailable", "missing", "identity changed", "ambiguous"}
logger = logging.getLogger(__name__)


def current_helper_identity() -> str | None:
    """Return the bundled Keychain helper's macOS cdhash as lowercase hex, or None.

    The helper is the code identity macOS binds the "Always Allow" grant to, and release
    signing keeps its cdhash fixed (``scripts/check_helper_cdhash.py``). The main app binary
    changes on every release, so it must not decide whether a grant was lost.
    """
    if sys.platform != "darwin":
        return None
    try:
        from scripts.sidecar_pkg.keychain_client import native_helper_path

        helper = os.fsencode(native_helper_path())
        if not os.path.isfile(helper):
            logger.debug("Keychain helper missing; code identity unavailable")
            return None
        sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ptr = ctypes.c_void_p
        cf.CFURLCreateFromFileSystemRepresentation.argtypes = [
            ptr,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_bool,
        ]
        cf.CFURLCreateFromFileSystemRepresentation.restype = ptr
        sec.SecStaticCodeCreateWithPath.argtypes = [ptr, ctypes.c_uint32, ctypes.POINTER(ptr)]
        sec.SecStaticCodeCreateWithPath.restype = ctypes.c_int32
        sec.SecCodeCopySigningInformation.argtypes = [ptr, ctypes.c_uint32, ctypes.POINTER(ptr)]
        sec.SecCodeCopySigningInformation.restype = ctypes.c_int32
        cf.CFDictionaryGetValue.argtypes = [ptr, ptr]
        cf.CFDictionaryGetValue.restype = ptr
        cf.CFGetTypeID.argtypes = [ptr]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFDataGetTypeID.argtypes = []
        cf.CFDataGetTypeID.restype = ctypes.c_ulong
        cf.CFDataGetLength.argtypes = [ptr]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetBytePtr.argtypes = [ptr]
        cf.CFDataGetBytePtr.restype = ptr
        cf.CFRelease.argtypes = [ptr]
        cf.CFRelease.restype = None

        url, code, info = ptr(), ptr(), ptr()
        try:
            url.value = cf.CFURLCreateFromFileSystemRepresentation(None, helper, len(helper), False)
            if not url:
                return None
            if sec.SecStaticCodeCreateWithPath(url, 0, ctypes.byref(code)) != 0 or not code:
                logger.debug("SecStaticCodeCreateWithPath failed; code identity unavailable")
                return None
            if sec.SecCodeCopySigningInformation(code, 2, ctypes.byref(info)) != 0 or not info:
                logger.debug("SecCodeCopySigningInformation failed; code identity unavailable")
                return None
            key = ptr.in_dll(sec, "kSecCodeInfoUnique")
            value = cf.CFDictionaryGetValue(info, key)
            if not value or cf.CFGetTypeID(value) != cf.CFDataGetTypeID():
                logger.debug("kSecCodeInfoUnique missing or not CFData; code identity unavailable")
                return None
            length = cf.CFDataGetLength(value)
            raw_ptr = cf.CFDataGetBytePtr(value)
            if length <= 0 or not raw_ptr:
                logger.debug("kSecCodeInfoUnique empty; code identity unavailable")
                return None
            return ctypes.string_at(raw_ptr, length).hex()
        finally:
            if info:
                cf.CFRelease(info)
            if code:
                cf.CFRelease(code)
            if url:
                cf.CFRelease(url)
    except Exception:
        logger.debug("Failed to read macOS code identity", exc_info=True)
        return None


@dataclass(frozen=True)
class ConsentTicket:
    source_id: str
    revision: str
    generation: int
    destination: str
    epoch: int


class SourcePolicy:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        code_identity: Callable[[], str | None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._config: dict[str, Any] = {}
        self._paused: set[str] = set()
        self._epochs: dict[str, int] = {}
        self._successful_reads: dict[str, float] = {}
        self._read_state: dict[str, dict[str, Any]] = {}
        self._state_path: Path | None = None
        self._code_identity = code_identity
        self.configure(config)

    def configure(self, config: dict[str, Any]) -> None:
        with self._lock:
            old = self._config
            self._config = copy.deepcopy(config)
            for source_id in catalog():
                if (self._entry(old, source_id), old.get("api_url")) != (
                    self._entry(config, source_id),
                    config.get("api_url"),
                ):
                    self._epochs[source_id] = self._epochs.get(source_id, 0) + 1
                    self._successful_reads.pop(source_id, None)
                    if self._read_state.pop(source_id, None) is not None:
                        self._persist_reads()

    @staticmethod
    def _entry(config: dict[str, Any], source_id: str) -> dict[str, Any]:
        sources = config.get("keychain_sources")
        entry = sources.get(source_id) if isinstance(sources, dict) else None
        return entry if isinstance(entry, dict) else {}

    def ticket(self, source_id: str) -> ConsentTicket | None:
        descriptor = catalog().get(source_id)
        if descriptor is None:
            return None
        with self._lock:
            entry = self._entry(self._config, source_id)
            generation = entry.get("generation", 0)
            destination = self._config.get("api_url", "")
            if (
                entry.get("read") is not True
                or entry.get("revision") != descriptor.revision
                or type(generation) is not int
                or generation < 0
                or entry.get("destination", "") != destination
                or source_id in self._paused
            ):
                return None
            return ConsentTicket(
                source_id,
                descriptor.revision,
                generation,
                destination,
                self._epochs.get(source_id, 0),
            )

    def current(self, ticket: ConsentTicket | None) -> bool:
        return ticket is not None and self.ticket(ticket.source_id) == ticket

    def pause(self, source_id: str, paused: bool) -> None:
        if source_id not in catalog():
            raise ValueError("Unknown scraper")
        with self._lock:
            if (source_id in self._paused) != paused:
                self._epochs[source_id] = self._epochs.get(source_id, 0) + 1
            if paused:
                self._paused.add(source_id)
            else:
                self._paused.discard(source_id)

    def configure_state(self, path: Path) -> None:
        """Load display evidence once; it can never create a consent ticket."""
        with self._lock:
            if self._state_path == path:
                return
            self._state_path = path
            self._read_state = {}
            self._successful_reads.clear()
            try:
                info = path.stat()
                if path.is_symlink() or (
                    os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
                ):
                    return
                saved = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    return
                for source_id, record in saved.items():
                    descriptor = catalog().get(source_id)
                    if descriptor is None or not isinstance(record, dict):
                        return
                    keys = set(record)
                    if not (
                        _REQUIRED_RECORD_KEYS
                        <= keys
                        <= (_REQUIRED_RECORD_KEYS | _OPTIONAL_RECORD_KEYS)
                    ):
                        return
                    if record["revision"] != descriptor.revision:
                        continue
                    for field in ("last_read_at", "last_block_at"):
                        value = record[field]
                        if value is not None and (
                            type(value) not in (int, float)
                            or not 0 < value < 253402300800
                            or not math.isfinite(value)
                        ):
                            return
                    reason = record["last_block_reason"]
                    if reason is not None and (
                        not isinstance(reason, str) or reason not in _BLOCK_REASONS
                    ):
                        return
                    if (reason is None) != (record["last_block_at"] is None):
                        return
                    for id_field in _OPTIONAL_RECORD_KEYS:
                        if id_field in record:
                            id_val = record[id_field]
                            if id_val is not None and (
                                not isinstance(id_val, str) or not 0 < len(id_val) <= 128
                            ):
                                return
                self._read_state = {
                    key: {k: v for k, v in value.items() if k not in _LEGACY_IDENTITY_KEYS}
                    for key, value in saved.items()
                    if value["revision"] == catalog()[key].revision
                }
            except (OSError, ValueError, TypeError):
                # No stale in-memory fallback after a corrupt/unreadable state file.
                self._read_state = {}

    def _persist_reads(self) -> None:
        if self._state_path is None:
            return
        temporary = None
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".source-reads-", dir=self._state_path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(self._read_state, output, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._state_path)
        except OSError:
            logger.warning("Could not persist Keychain read status")
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    # A successful os.replace already moved the temporary file.
                    pass

    def _read_record(self, source_id: str) -> dict[str, Any]:
        record = self._read_state.get(source_id, {})
        descriptor = catalog().get(source_id)
        return record if descriptor and record.get("revision") == descriptor.revision else {}

    def _current_identity(self) -> str | None:
        provider = (
            self._code_identity if self._code_identity is not None else current_helper_identity
        )
        value = provider()
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _with_identity_fields(record: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
        for field in _IDENTITY_KEYS:
            if existing.get(field) is not None:
                record[field] = existing[field]
        return record

    def record_read(self, ticket: ConsentTicket) -> None:
        with self._lock:
            if self.current(ticket):
                now = time.time()
                existing = self._read_record(ticket.source_id)
                self._successful_reads[ticket.source_id] = now
                record: dict[str, Any] = {
                    "revision": ticket.revision,
                    "last_read_at": now,
                    "last_block_reason": existing.get("last_block_reason"),
                    "last_block_at": existing.get("last_block_at"),
                }
                self._with_identity_fields(record, existing)
                current = self._current_identity()
                if current is not None:
                    # A successful read proves the helper can reach the item, so record its
                    # identity (also for records that predate tracking it).
                    record["helper_identity"] = current
                self._read_state[ticket.source_id] = record
                self._persist_reads()

    def record_authorization(self, ticket: ConsentTicket, code_identity: str | None = None) -> None:
        """Record the non-secret code identity that authorized this source."""
        with self._lock:
            if not self.current(ticket):
                return
            identity = code_identity if code_identity is not None else self._current_identity()
            if not isinstance(identity, str) or not identity:
                return
            existing = self._read_record(ticket.source_id)
            record: dict[str, Any] = {
                "revision": ticket.revision,
                "last_read_at": existing.get("last_read_at"),
                "last_block_reason": existing.get("last_block_reason"),
                "last_block_at": existing.get("last_block_at"),
                "helper_identity": identity,
            }
            self._read_state[ticket.source_id] = record
            self._persist_reads()

    def record_block(self, ticket: ConsentTicket, reason: str) -> None:
        with self._lock:
            if self.current(ticket) and reason in _BLOCK_REASONS:
                existing = self._read_record(ticket.source_id)
                self._successful_reads.pop(ticket.source_id, None)
                self._read_state[ticket.source_id] = self._with_identity_fields(
                    {
                        "revision": ticket.revision,
                        "last_read_at": existing.get("last_read_at"),
                        "last_block_reason": reason,
                        "last_block_at": time.time(),
                    },
                    existing,
                )
                self._persist_reads()

    def needs_reauthorization(self, source_id: str) -> bool:
        """True when an enabled source was authorized under a different code identity."""
        with self._lock:
            if self.ticket(source_id) is None or source_id in self._successful_reads:
                return False
            recorded = self._read_record(source_id).get("helper_identity")
            if not isinstance(recorded, str) or not recorded:
                return False
            current = self._current_identity()
            return current is not None and recorded != current

    def reauthorization_count(self) -> int:
        with self._lock:
            return sum(1 for source_id in catalog() if self.needs_reauthorization(source_id))

    def pending_reauthorization_notice(self) -> tuple[str, str] | None:
        """Return (message, code_identity) if any source needs a notice for this identity."""
        with self._lock:
            current = self._current_identity()
            if current is None:
                return None
            affected = [
                source_id for source_id in catalog() if self.needs_reauthorization(source_id)
            ]
            if not affected:
                return None
            if not any(
                self._read_record(source_id).get("notified_helper_identity") != current
                for source_id in affected
            ):
                return None
            count = len(affected)
            noun = "source needs" if count == 1 else "sources need"
            return (
                f"{count} {noun} re-authorization after the update. Open Settings to re-authorize.",
                current,
            )

    def acknowledge_reauthorization_notice(self, code_identity: str) -> None:
        """Record that a re-authorization notice was delivered for *code_identity*."""
        with self._lock:
            if not isinstance(code_identity, str) or not code_identity:
                return
            changed = False
            for source_id in catalog():
                if not self.needs_reauthorization(source_id):
                    continue
                record = dict(self._read_record(source_id))
                if record and record.get("notified_helper_identity") != code_identity:
                    record["notified_helper_identity"] = code_identity
                    self._read_state[source_id] = record
                    changed = True
            if changed:
                self._persist_reads()

    def reauthorization_notice(self) -> str | None:
        """Return and acknowledge a one-time notice for the current code identity."""
        with self._lock:
            pending = self.pending_reauthorization_notice()
            if pending is None:
                return None
            message, current = pending
            self.acknowledge_reauthorization_notice(current)
            return message

    def last_read(self, source_id: str) -> float | None:
        with self._lock:
            if self.ticket(source_id) is None:
                return None
            value = self._read_record(source_id).get("last_read_at")
            return float(value) if value is not None else None

    def read_state(self, source_id: str) -> str | None:
        """Current outcome first, then bounded restart evidence; never do native IO."""
        with self._lock:
            if self.ticket(source_id) is None:
                return None
            if source_id in self._successful_reads and self._read_record(source_id):
                return "Authorized"
            if self.needs_reauthorization(source_id):
                return REAUTH_REQUIRED_STATE
            record = self._read_record(source_id)
            if record.get("last_block_reason") and (
                record.get("last_read_at") is None
                or record["last_block_at"] >= record["last_read_at"]
            ):
                return "Action required"
            last = record.get("last_read_at")
            if last is not None and 0 <= time.time() - last <= READ_STATE_MAX_AGE:
                checked = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last))
                return f"Authorized (checked {checked})"
            return None

    def block_reason(self, source_id: str) -> str | None:
        """Return the persisted fixed block reason when this source is Action required."""
        with self._lock:
            if self.read_state(source_id) != "Action required":
                return None
            reason = self._read_record(source_id).get("last_block_reason")
            return reason if reason in _BLOCK_REASONS else None

    def renewal_allowed(self) -> bool:
        with self._lock:
            return (
                self._config.get("claude_keychain_renewal") is True
                and self.ticket("anthropic.claude-code") is not None
            )

    @staticmethod
    def change(config: dict[str, Any], source_id: str, *, read: bool) -> dict[str, Any]:
        descriptor = catalog().get(source_id)
        if descriptor is None:
            raise ValueError("Unknown scraper")
        if type(read) is not bool:
            raise ValueError("Read permission must be boolean")
        updated = copy.deepcopy(config)
        sources = updated.get("keychain_sources")
        sources = sources if isinstance(sources, dict) else {}
        updated["keychain_sources"] = sources
        previous = SourcePolicy._entry(config, source_id)
        generation = previous.get("generation", 0)
        generation = generation if type(generation) is int and generation >= 0 else 0
        sources[source_id] = {
            "read": read,
            "revision": descriptor.revision,
            "generation": generation + 1,
            "destination": config.get("api_url", ""),
        }
        if not read and source_id == "anthropic.claude-code":
            updated["claude_keychain_renewal"] = False
        return updated

    @staticmethod
    def enable_all(config: dict[str, Any]) -> dict[str, Any]:
        updated = copy.deepcopy(config)
        for source_id in catalog():
            updated = SourcePolicy.change(updated, source_id, read=True)
        return updated


POLICY = SourcePolicy({})
_RULE: ContextVar[tuple[str, dict[str, Any]] | None] = ContextVar("keychain_rule", default=None)


@contextmanager
def rule_scope(provider: str, rule: dict[str, Any]) -> Iterator[None]:
    token = _RULE.set((provider, copy.deepcopy(rule)) if trusted_rule(provider, rule) else None)
    try:
        yield
    finally:
        _RULE.reset(token)


def service_ticket(service: str) -> ConsentTicket | None:
    if service == "Claude Code-credentials" or service.startswith("Claude Code-credentials-"):
        return POLICY.ticket("anthropic.claude-code")
    scoped = _RULE.get()
    if scoped is None:
        return None
    provider, rule = scoped
    if service == "gh:github.com" and provider == "github" and rule.get("type") == "exec":
        return POLICY.ticket("github.gh-cli")
    if service == "gemini" and provider == "antigravity" and rule.get("type") == "keychain":
        return POLICY.ticket("antigravity.agy-cli")
    suffix = {
        "Chrome Safe Storage": "chrome",
        "Chromium Safe Storage": "chromium",
        "Microsoft Edge Safe Storage": "edge",
    }.get(service)
    if service == "Claude Safe Storage" and provider == "anthropic":
        suffix = (
            "desktop-oauth" if rule.get("type") == "claude_desktop_oauth" else "desktop-session"
        )
    if suffix is None:
        return None
    source_id = f"{provider}.{suffix}"
    descriptor = catalog().get(source_id)
    return (
        POLICY.ticket(source_id)
        if descriptor is not None and service in descriptor.services
        else None
    )


def cookie_ticket(service: str, domain: str, name: str) -> ConsentTicket | None:
    scoped = _RULE.get()
    if scoped is None:
        return None
    _provider, rule = scoped
    if rule.get("type") != "cookie" or name != rule.get("name"):
        return None
    if domain.lstrip(".") not in {d.lstrip(".") for d in rule.get("domains", [])}:
        return None
    return service_ticket(service)


def stamp(ticket: ConsentTicket) -> dict[str, Any]:
    return {
        "source_id": ticket.source_id,
        "revision": ticket.revision,
        "generation": ticket.generation,
        "destination": ticket.destination,
        "epoch": ticket.epoch,
    }


def valid_stamp(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        return POLICY.current(ConsentTicket(**value))
    except (TypeError, ValueError):
        return False


def cookie_request_scope(function: Any) -> Any:
    """Bind public compatibility calls to catalogued domains, cookie and provider."""
    from scripts.sidecar_pkg.source_catalog_data import KEYCHAIN_RULES

    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        provider = bound.arguments.get("provider")
        domains = bound.arguments.get("domains") or [bound.arguments.get("domain")]
        name = bound.arguments.get("name")
        for provider_id, spec in KEYCHAIN_RULES.items():
            if provider is not None and provider != provider_id:
                continue
            for rule in spec["rules"]:
                if (
                    rule.get("type") == "cookie"
                    and rule.get("name") == name
                    and all(
                        isinstance(domain, str) and domain in rule.get("domains", [])
                        for domain in domains
                    )
                ):
                    with rule_scope(provider_id, rule):
                        return function(*args, **kwargs)
        with rule_scope("unknown", {}):
            return function(*args, **kwargs)

    return wrapped
