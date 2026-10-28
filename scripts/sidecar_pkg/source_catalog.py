"""Static, generated Keychain resource catalog; constructing it performs no discovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from scripts.sidecar_pkg.source_catalog_data import KEYCHAIN_RULES


@dataclass(frozen=True)
class SourceDescriptor:
    id: str
    provider: str
    label: str
    services: tuple[str, ...]
    revision: str
    renewal: bool = False
    platforms: tuple[str, ...] = ("darwin",)
    account: str | None = None


def catalog() -> dict[str, SourceDescriptor]:
    result = {}
    for provider, spec in KEYCHAIN_RULES.items():
        rules = spec["rules"]
        for suffix, label, services, matches, renewal in [
            (
                "gh-cli",
                "GitHub CLI",
                ("gh:github.com",),
                [
                    r
                    for r in rules
                    if r["type"] == "exec"
                    and r.get("command") == ["gh", "auth", "token", "--hostname", "github.com"]
                ]
                if provider == "github"
                else [],
                False,
            ),
            (
                "claude-code",
                "Claude Code",
                ("Claude Code-credentials",),
                [r for r in rules if r["type"] == "claude_code_keychain"],
                True,
            ),
            (
                "desktop-oauth",
                "Claude Desktop OAuth",
                ("Claude Safe Storage",),
                [r for r in rules if r["type"] == "claude_desktop_oauth"],
                False,
            ),
            (
                "agy-cli",
                "Antigravity CLI",
                ("gemini",),
                [r for r in rules if r["type"] == "keychain" and r.get("service_name") == "gemini"]
                if provider == "antigravity"
                else [],
                False,
            ),
            *[
                (
                    browser.lower(),
                    f"{spec['name']} · {browser} cookies",
                    (service,),
                    [r for r in rules if r["type"] == "cookie"],
                    False,
                )
                for browser, service in [
                    ("Chrome", "Chrome Safe Storage"),
                    ("Chromium", "Chromium Safe Storage"),
                    ("Edge", "Microsoft Edge Safe Storage"),
                ]
            ],
            (
                "desktop-session",
                "Claude Desktop session",
                ("Claude Safe Storage",),
                [r for r in rules if r["type"] == "cookie"] if provider == "anthropic" else [],
                False,
            ),
        ]:
            if not matches:
                continue
            # The account picks the entry inside a service, so it comes from the
            # rule that reads it: the descriptor and the rule can never drift
            # apart, and the revision covers both.
            account = next(
                (
                    rule.get("account")
                    for rule in matches
                    if isinstance(rule, dict) and isinstance(rule.get("account"), str)
                ),
                None,
            )
            source_id = f"{provider}.{suffix}"
            platforms: tuple[str, ...] = ("darwin",)
            revision = hashlib.sha256(
                json.dumps(
                    [services, matches, {"renewal": renewal, "platforms": list(platforms)}],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            result[source_id] = SourceDescriptor(
                source_id, provider, label, services, revision, renewal, platforms, account
            )
    return result


# Lifecycle metadata for existing Windows collectors; these are deliberately not
# part of catalog(), the local macOS Keychain permission/authorization catalog.
_WINDOWS_CREDENTIALS = (
    ("github", "git:https://github.com", "Git Credential Manager"),
    ("antigravity", "gemini:antigravity", "Antigravity Credential Manager"),
)


def windows_source_for_origin(provider: str, origin: str) -> str | None:
    base = origin.split("#", 1)[0]
    for expected, target, _label in _WINDOWS_CREDENTIALS:
        if provider == expected and base == "keychain:" + target:
            return provider + ".windows-credential"
    return None


def control_catalog() -> dict[str, SourceDescriptor]:
    """All Fleet lifecycle sources; this never expands local Keychain consent."""
    result = catalog()
    for provider, target, label in _WINDOWS_CREDENTIALS:
        source_id = provider + ".windows-credential"
        revision = hashlib.sha256(
            json.dumps(["windows_credential", provider, target]).encode()
        ).hexdigest()
        result[source_id] = SourceDescriptor(
            source_id, provider, label, (target,), revision, platforms=("win32",)
        )
    return result


def trusted_rule(provider: str, rule: dict[str, Any]) -> bool:
    for allowed in KEYCHAIN_RULES.get(provider, {}).get("rules", []):
        candidate, baseline = dict(rule), dict(allowed)
        domains = candidate.pop("domains", None)
        allowed_domains = baseline.pop("domains", None)
        if domains is not None or allowed_domains is not None:
            if (
                not isinstance(domains, list)
                or not domains
                or not isinstance(allowed_domains, list)
                or any(
                    not isinstance(domain, str) or domain not in allowed_domains
                    for domain in domains
                )
            ):
                continue
        if candidate == baseline:
            return True
    return False


def source_for_origin(provider: str, origin: str) -> str | None:
    base = origin.split("#", 1)[0]
    if base == "keychain:Claude Code-credentials" or base.startswith(
        "keychain:Claude Code-credentials-"
    ):
        return "anthropic.claude-code"
    if base == "keychain:GitHub CLI":
        return "github.gh-cli"
    if base.startswith("keychain:Claude Desktop/oauth/"):
        return "anthropic.desktop-oauth"
    if base == "keychain:gemini:antigravity":
        return "antigravity.agy-cli"
    for browser, suffix in [
        ("Chrome", "chrome"),
        ("Chromium", "chromium"),
        ("Edge", "edge"),
        ("Claude Desktop", "desktop-session"),
    ]:
        if base.startswith(f"cookie:{provider}/{browser}/"):
            source_id = f"{provider}.{suffix}"
            return source_id if source_id in catalog() else "unknown"
    parts = base.removeprefix("cookie:").split("/")
    native_browser = (
        base.startswith("cookie:")
        and len(parts) >= 3
        and parts[1] in {"Chrome", "Chromium", "Edge", "Claude Desktop"}
    )
    return "unknown" if base.startswith("keychain:") or native_browser else None
