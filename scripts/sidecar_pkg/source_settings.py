"""Secret-free local scraper settings and capability reporting."""

from __future__ import annotations

import sys
from typing import Any

from scripts.sidecar_pkg.source_catalog import BROWSERS, catalog
from scripts.sidecar_pkg.source_consent import SourcePolicy


def report(config: dict[str, Any]) -> dict[str, Any]:
    from scripts.sidecar_pkg import source_protocol
    from scripts.sidecar_pkg.source_consent import POLICY
    from scripts.sidecar_pkg.source_setup import SETUP

    client = source_protocol.CLIENT
    client = (
        client
        if client is not None
        and client.scope == (config.get("device_id"), config.get("device_api_url"))
        else None
    )
    remote = client.state.get("commands", {}) if client else {}
    withdrawals = client.state.get("withdrawals", {}) if client else {}
    policy = SourcePolicy(config)
    dismissed = config.get("keychain_announcements", {})
    dismissed = dismissed if isinstance(dismissed, dict) else {}
    pending = config.get("keychain_withdrawals", {})
    pending = pending if isinstance(pending, dict) else {}
    return {
        "compatible": any(sys.platform in source.platforms for source in catalog().values()),
        "sources": [
            {
                "id": source.id,
                "compatible": sys.platform in source.platforms,
                "label": source.label,
                # How the page groups it: apps and CLIs one row each, cookie sources one row per
                # service. Sources with the same ``item`` read the same Keychain item.
                "group": source.browser_key or "app",
                "service": source.service,
                "item": source.item_key,
                "revision": source.revision,
                "read": policy.ticket(source.id) is not None,
                "state": _authorization_state(source.id, source.services, SETUP.state(source.id))
                if policy.ticket(source.id)
                else "Withdrawal pending"
                if source.id in pending and pending[source.id] > withdrawals.get(source.id, -1)
                else "Off",
                "last_read_at": POLICY.last_read(source.id),
                "remote_paused": remote.get(source.id, {}).get("paused") is True,
                "revoked": _is_revoked(config, remote, source.id),
                "setup_requested": remote.get(source.id, {}).get("setup_requested") is True,
                "renewal_requested": source.renewal
                and config.get("claude_keychain_renewal") is True,
                "renewal_supported": False,
                "dependency": "Keychain: " + ", ".join(source.services),
                "new": sys.platform in source.platforms
                and dismissed.get(source.id) != source.revision
                and policy.ticket(source.id) is None,
            }
            for source in catalog().values()
        ],
        "browsers": browsers(config),
        "run": SETUP.run_report(),
        "forwarding_supported": bool(client and client.accepted),
        "re_pair_required": bool(
            (client and client.re_pair_required)
            or (
                config.get("device_id")
                and config.get("device_api_url", "").rstrip("/")
                != config.get("api_url", "").rstrip("/")
            )
        ),
        "setup_available": sys.platform == "darwin" and getattr(sys, "frozen", False),
    }


def browsers(config: dict[str, Any]) -> dict[str, bool]:
    """Which browsers' cookie stores may be read; every browser is selected until it is unticked."""
    saved = config.get("keychain_browsers")
    saved = saved if isinstance(saved, dict) else {}
    return {key: saved.get(key) is not False for key in BROWSERS}


def update(config: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(command, dict):
        raise ValueError("Invalid source action")
    source_id, action = command.get("source_id"), command.get("action")
    updated = dict(config)
    if action == "enable_all":
        selected = browsers(config)
        for source_id, descriptor in catalog().items():
            if descriptor.browser_key is None or selected[descriptor.browser_key]:
                updated = SourcePolicy.change(updated, source_id, read=True)
        return updated
    if action == "set_sources":
        ids, value = command.get("source_ids"), command.get("value")
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(i, str) or i not in catalog() for i in ids)
            or type(value) is not bool
        ):
            raise ValueError("Invalid source selection")
        for source_id in dict.fromkeys(ids):
            updated = update(
                updated, {"source_id": source_id, "action": "enable" if value else "disable"}
            )
        return updated
    if action == "set_browsers":
        value = command.get("value")
        if (
            not isinstance(value, dict)
            or not value
            or any(key not in BROWSERS or type(flag) is not bool for key, flag in value.items())
        ):
            raise ValueError("Invalid browser selection")
        updated["keychain_browsers"] = {**browsers(config), **value}
        # A browser that is no longer read must not keep its sources on.
        for source_id, descriptor in catalog().items():
            if descriptor.browser_key and value.get(descriptor.browser_key) is False:
                if SourcePolicy(updated).ticket(source_id) is not None:
                    updated = update(updated, {"source_id": source_id, "action": "disable"})
        return updated
    if action == "disable_all":
        for source_id in catalog():
            updated = update(updated, {"source_id": source_id, "action": "disable"})
        return updated
    if action == "renewal":
        if type(command.get("value")) is not bool:
            raise ValueError("Renewal permission must be boolean")
        if command["value"] and SourcePolicy(config).ticket("anthropic.claude-code") is None:
            raise ValueError("Enable Claude Code Keychain reading first")
        updated["claude_keychain_renewal"] = command["value"]
        return updated
    if not isinstance(source_id, str) or source_id not in catalog():
        raise ValueError("Unknown scraper")
    if action in {"enable", "disable"}:
        updated = SourcePolicy.change(config, source_id, read=action == "enable")
        if action == "disable":
            pending = config.get("keychain_withdrawals", {})
            pending = dict(pending) if isinstance(pending, dict) else {}
            pending[source_id] = updated["keychain_sources"][source_id]["generation"]
            updated["keychain_withdrawals"] = pending
        return updated
    if action == "dismiss":
        dismissed = config.get("keychain_announcements", {})
        dismissed = dict(dismissed) if isinstance(dismissed, dict) else {}
        dismissed[source_id] = catalog()[source_id].revision
        updated["keychain_announcements"] = dismissed
        return updated
    raise ValueError("Invalid source action")


def _is_revoked(config: dict[str, Any], remote: dict[str, Any], source_id: str) -> bool:
    generation = SourcePolicy._entry(config, source_id).get("generation", 0)
    revoked = remote.get(source_id, {}).get("revoked_through", -1)
    return type(generation) is int and type(revoked) is int and generation <= revoked


def _authorization_state(source_id: str, services: tuple[str, ...], setup_state: str) -> str:
    from scripts.sidecar_pkg.keychain_client import access_groups_blocked

    if setup_state == "Connecting":
        return setup_state
    groups = [
        "claude-code"
        if source_id == "anthropic.claude-code"
        else "github-cli"
        if source_id == "github.gh-cli"
        else "storage:" + service
        for service in services
    ]
    if access_groups_blocked(groups):
        return "Action required"
    return setup_state if setup_state != "Off" else "Setup required"
