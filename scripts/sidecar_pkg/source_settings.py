"""Secret-free local scraper settings and capability reporting."""

from __future__ import annotations

import sys
from typing import Any

from scripts.sidecar_pkg.source_catalog import catalog
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
        "sources": [
            {
                "id": source.id,
                "label": source.label,
                "revision": source.revision,
                "read": policy.ticket(source.id) is not None,
                "state": (
                    SETUP.state(source.id) if SETUP.state(source.id) != "Off" else "Setup required"
                )
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
                "new": dismissed.get(source.id) != source.revision
                and policy.ticket(source.id) is None,
            }
            for source in catalog().values()
        ],
        "forwarding_supported": bool(client and client.accepted),
        "setup_available": sys.platform == "darwin" and getattr(sys, "frozen", False),
    }


def update(config: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(command, dict):
        raise ValueError("Invalid source action")
    source_id, action = command.get("source_id"), command.get("action")
    updated = dict(config)
    if action == "enable_all":
        return SourcePolicy.enable_all(config)
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
