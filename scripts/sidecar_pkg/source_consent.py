"""Local Keychain consent. Fleet can narrow permission, never grant it."""

from __future__ import annotations

import copy
import inspect
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

from scripts.sidecar_pkg.source_catalog import catalog, trusted_rule


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


def source_for_origin(provider: str, origin: str) -> str | None:
    base = origin.split("#", 1)[0]
    if base.startswith("keychain:Claude Code-credentials"):
        return "anthropic.claude-code"
    if base == "keychain:GitHub CLI":
        return "github.gh-cli"
    if base.startswith("keychain:Claude Desktop/oauth/"):
        return "anthropic.desktop-oauth"
    for browser, suffix in [
        ("Chrome", "chrome"),
        ("Chromium", "chromium"),
        ("Edge", "edge"),
        ("Claude Desktop", "desktop-session"),
    ]:
        if base.startswith(f"cookie:{provider}/{browser}/"):
            source_id = f"{provider}.{suffix}"
            return source_id if source_id in catalog() else "unknown"
    return "unknown" if base.startswith("keychain:") else None


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


def filter_egress(data: dict[str, Any]) -> dict[str, Any]:
    """Withhold Keychain contributions until authenticated withdrawal is available."""

    def keychain_origin(value: Any) -> bool:
        return isinstance(value, dict) and (
            "keychain_consent" in value
            or (
                sys.platform == "darwin"
                and bool(
                    source_for_origin(
                        str(value.get("provider_id", "")), str(value.get("credential_origin", ""))
                    )
                )
            )
        )

    result = dict(data)
    if isinstance(data.get("metrics"), list):
        result["metrics"] = [
            card
            for card in data["metrics"]
            if not isinstance(card, dict) or not keychain_origin(card.get("metadata"))
        ]
    for field in ("entries", "observations"):
        if isinstance(data.get(field), list):
            result[field] = [item for item in data[field] if not keychain_origin(item)]
    if (
        sys.platform == "darwin"
        and "entries" in data
        and isinstance(data.get("completed_providers"), list)
    ):
        # Withholding a source or refusing permission is not proof of logout.
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
