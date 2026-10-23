"""Local Keychain consent. Fleet can narrow permission, never grant it."""

from __future__ import annotations

import copy
import inspect
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

from scripts.sidecar_pkg.source_catalog import catalog, source_for_origin, trusted_rule


@dataclass(frozen=True)
class ConsentTicket:
    source_id: str
    revision: str
    generation: int
    destination: str
    epoch: int


class SourcePolicy:
    def __init__(self, config: dict[str, Any]) -> None:
        self._lock = threading.RLock()
        self._config: dict[str, Any] = {}
        self._paused: set[str] = set()
        self._epochs: dict[str, int] = {}
        self._successful_reads: dict[str, float] = {}
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

    def record_read(self, ticket: ConsentTicket) -> None:
        with self._lock:
            if self.current(ticket):
                self._successful_reads[ticket.source_id] = time.time()

    def last_read(self, source_id: str) -> float | None:
        with self._lock:
            return self._successful_reads.get(source_id)

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


def filter_egress(data: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recheck local consent and negotiated withdrawal support immediately at egress."""
    from scripts.sidecar_pkg.source_protocol import device_credentials, forwarding_allowed

    config = config or {}
    destination = str(config.get("api_url", ""))
    authenticated = (
        device_credentials(config, destination.rstrip("/") + "/api/v1/fleet/ingest") is not None
    )

    def sanitize(value: Any, provider_override: Any = None) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        provider, origin = (
            str(provider_override or value.get("provider_id", "")),
            str(value.get("credential_origin", "")),
        )
        source_id = source_for_origin(provider, origin)
        proof = value.get("keychain_consent")
        keychain = "keychain_consent" in value or (sys.platform == "darwin" and bool(source_id))
        if keychain:
            if (
                not authenticated
                or not isinstance(proof, dict)
                or not valid_stamp(proof)
                or proof.get("source_id") != source_id
                or not forwarding_allowed(str(source_id), destination)
            ):
                return None
            result["keychain_consent"] = {
                key: proof[key] for key in ("source_id", "revision", "generation")
            }
        if provider == "anthropic" and (
            "refresh_token" in value or value.get("oauth_read_only") == "1"
        ):
            result.pop("refresh_token", None)
            result["oauth_read_only"] = "1"
        return result

    result = dict(data)
    if isinstance(data.get("metrics"), list):
        cards = []
        for card in data["metrics"]:
            if not isinstance(card, dict) or not isinstance(card.get("metadata"), dict):
                cards.append(card)
                continue
            metadata = sanitize(card["metadata"], card.get("provider_id"))
            if metadata is not None:
                cards.append({**card, "metadata": metadata})
        result["metrics"] = cards
    for field in ("entries", "observations"):
        if isinstance(data.get(field), list):
            result[field] = [clean for item in data[field] if (clean := sanitize(item)) is not None]
    if (
        sys.platform == "darwin"
        and "entries" in data
        and isinstance(data.get("completed_providers"), list)
    ):
        # An incomplete/withheld native scan never proves logout. Explicit generation
        # withdrawal owns retirement; files still report their own live contributions.
        protected = {source.provider for source in catalog().values()}
        result["completed_providers"] = [
            provider for provider in data["completed_providers"] if provider not in protected
        ]
    return result


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
