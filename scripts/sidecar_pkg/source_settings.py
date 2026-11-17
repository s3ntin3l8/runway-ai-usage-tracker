"""Secret-free local scraper settings and capability reporting."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.source_catalog import BROWSERS, catalog
from scripts.sidecar_pkg.source_consent import SourcePolicy

_BROWSER_INSTALL_TARGETS: dict[str, tuple[str, str]] = {
    "chrome": ("Google Chrome.app", "Google/Chrome"),
    "edge": ("Microsoft Edge.app", "Microsoft Edge"),
    "chromium": ("Chromium.app", "Chromium"),
}
_APPLICATIONS_DIR = Path("/Applications")


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def installed_browsers(
    home: Path | None = None, applications_dir: Path | None = None
) -> dict[str, bool]:
    """Detect which supported Chromium browsers are installed on macOS."""
    if sys.platform != "darwin":
        return dict.fromkeys(BROWSERS, False)
    user_home = home if home is not None else Path.home()
    apps_dir = applications_dir if applications_dir is not None else _APPLICATIONS_DIR
    return {
        key: (
            (target := _BROWSER_INSTALL_TARGETS.get(key)) is not None
            and (
                _path_exists(apps_dir / target[0])
                or _path_exists(user_home / "Applications" / target[0])
                or _path_exists(user_home / "Library" / "Application Support" / target[1])
            )
        )
        for key in BROWSERS
    }


_SAFARI_STATE_LABELS = {
    "full_disk_access_missing": "Full Disk Access needed",
    "readable": "Readable",
    "not_used": "Safari not used",
}


def _safari_probe_state(*, persist: bool = True) -> str:
    if sys.platform != "darwin":
        return "not_used"
    from scripts.sidecar_pkg import safari_cookies

    try:
        return safari_cookies.probe_access(persist=persist)
    except TypeError:
        return safari_cookies.probe_access()


def _sync_safari_state_from_disk(config: dict[str, Any], *, from_update: bool = False) -> None:
    if sys.platform != "darwin":
        return
    import json

    from scripts.sidecar_pkg import safari_cookies

    try:
        saved = json.loads(safari_cookies._sidecar_config_path().read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            disk_last = saved.get("safari_fda_last_state")
            disk_dismissed = saved.get("safari_fda_notice_dismissed_state")
            mem_last = config.get("safari_fda_last_state")
            mem_dismissed = config.get("safari_fda_notice_dismissed_state")
            if isinstance(disk_last, str) and (
                from_update or not isinstance(mem_last, str) or disk_dismissed == ""
            ):
                config["safari_fda_last_state"] = disk_last
            if isinstance(disk_dismissed, str) and (
                not isinstance(mem_dismissed, str)
                or disk_dismissed == ""
                or (from_update and disk_last == disk_dismissed)
            ):
                config["safari_fda_notice_dismissed_state"] = disk_dismissed
    except (OSError, ValueError):
        # Missing or malformed on-disk config falls back to the in-memory config dict.
        pass


def _safari_report(config: dict[str, Any]) -> dict[str, Any]:
    _sync_safari_state_from_disk(config)
    config.pop("safari_fda_notice_rearmed", None)
    probe_state = _safari_probe_state(persist=False)
    dismissed = config.get("safari_fda_notice_dismissed_state")
    last_state = config.get("safari_fda_last_state")
    if (
        sys.platform == "darwin"
        and dismissed == "full_disk_access_missing"
        and (
            probe_state != "full_disk_access_missing"
            or (isinstance(last_state, str) and last_state != "full_disk_access_missing")
        )
    ):
        dismissed = ""
        config["safari_fda_notice_dismissed_state"] = ""
    notice = (
        sys.platform == "darwin"
        and probe_state == "full_disk_access_missing"
        and (
            dismissed != probe_state or (isinstance(last_state, str) and last_state != probe_state)
        )
    )
    config["safari_fda_last_state"] = probe_state
    if sys.platform == "darwin":
        from scripts.sidecar_pkg import safari_cookies

        safari_cookies._persist_last_state(
            probe_state,
            config,
            dismissed=dismissed if isinstance(dismissed, str) else None,
        )
    return {
        "id": "safari",
        "label": "Safari",
        "probe_state": probe_state,
        "state": _SAFARI_STATE_LABELS[probe_state],
        "notice": notice,
    }


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
    inst = installed_browsers()
    selected = browsers(config, installed=inst)
    sources = []
    for source in catalog().values():
        browser_key = source.browser_key
        browser_installed = True if browser_key is None else inst.get(browser_key, False)
        browser_selected = True if browser_key is None else selected[browser_key]
        has_ticket = policy.ticket(source.id) is not None
        withdrawal_pending = source.id in pending and pending[source.id] > withdrawals.get(
            source.id, -1
        )
        if has_ticket:
            state = _authorization_state(source.id, source.services, SETUP.state(source.id))
        elif withdrawal_pending:
            state = "Withdrawal pending"
        elif (
            sys.platform == "darwin"
            and browser_key is not None
            and not browser_installed
            and not browser_selected
        ):
            state = "Not installed"
        else:
            state = "Off"
        sources.append(
            {
                "id": source.id,
                "compatible": sys.platform in source.platforms,
                "label": source.label,
                # How the page groups it: apps and CLIs one row each, cookie sources one row per
                # service. Sources with the same ``item`` read the same Keychain item.
                "group": browser_key or "app",
                "service": source.service,
                "item": source.item_key,
                "revision": source.revision,
                "read": has_ticket,
                "state": state,
                "last_read_at": POLICY.last_read(source.id),
                "remote_paused": remote.get(source.id, {}).get("paused") is True,
                "revoked": _is_revoked(config, remote, source.id),
                "setup_requested": remote.get(source.id, {}).get("setup_requested") is True,
                "renewal_requested": source.renewal
                and config.get("claude_keychain_renewal") is True,
                "renewal_supported": False,
                "dependency": "Keychain: " + ", ".join(source.services),
                "new": sys.platform in source.platforms
                and (browser_installed or browser_selected)
                and dismissed.get(source.id) != source.revision
                and not has_ticket,
            }
        )
    return {
        "compatible": any(sys.platform in source.platforms for source in catalog().values()),
        "sources": sources,
        "installed": inst,
        "browsers": selected,
        "safari": _safari_report(config),
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


def browsers(
    config: dict[str, Any], *, installed: dict[str, bool] | None = None
) -> dict[str, bool]:
    """Which browsers' cookie stores may be read; uninstalled macOS browsers are off unless ticked."""
    saved = config.get("keychain_browsers")
    saved = saved if isinstance(saved, dict) else {}
    if sys.platform != "darwin":
        return {key: saved.get(key) is not False for key in BROWSERS}
    inst = installed if installed is not None else installed_browsers()
    result: dict[str, bool] = {}
    for key in BROWSERS:
        override = saved.get(key)
        if type(override) is bool:
            result[key] = override
        else:
            result[key] = bool(inst.get(key, False))
    return result


def update(config: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(command, dict):
        raise ValueError("Invalid source action")
    source_id, action = command.get("source_id"), command.get("action")
    updated = dict(config)
    _sync_safari_state_from_disk(updated, from_update=True)
    updated.pop("safari_fda_notice_rearmed", None)
    if action == "enable_all":
        selected = browsers(updated)
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
        selected = browsers(config)
        cat = catalog()
        for source_id in dict.fromkeys(ids):
            browser_key = cat[source_id].browser_key
            if value and browser_key is not None and not selected[browser_key]:
                continue
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
        prior = updated.get("keychain_browsers")
        updated["keychain_browsers"] = {**(prior if isinstance(prior, dict) else {}), **value}
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
        if command["value"] and SourcePolicy(updated).ticket("anthropic.claude-code") is None:
            raise ValueError("Enable Claude Code Keychain reading first")
        updated["claude_keychain_renewal"] = command["value"]
        return updated
    if action == "dismiss_safari_notice":
        probe_state = _safari_probe_state(persist=False)
        updated["safari_fda_notice_dismissed_state"] = probe_state
        updated["safari_fda_last_state"] = probe_state
        if sys.platform == "darwin":
            from scripts.sidecar_pkg import safari_cookies

            safari_cookies._persist_last_state(probe_state, updated, dismissed=probe_state)
        return updated
    if not isinstance(source_id, str) or source_id not in catalog():
        raise ValueError("Unknown scraper")
    if action in {"enable", "disable"}:
        updated = SourcePolicy.change(updated, source_id, read=action == "enable")
        if action == "disable":
            pending = updated.get("keychain_withdrawals", {})
            pending = dict(pending) if isinstance(pending, dict) else {}
            pending[source_id] = updated["keychain_sources"][source_id]["generation"]
            updated["keychain_withdrawals"] = pending
        return updated
    if action == "dismiss":
        dismissed = updated.get("keychain_announcements", {})
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
