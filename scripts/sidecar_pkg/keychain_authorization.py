"""User-invoked macOS consent read; never dispatched by the background worker."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.claude_code_keychain import account_name, service_candidates
from scripts.sidecar_pkg.keychain_protocol import KeychainError, keychain_reason
from scripts.sidecar_pkg.macos_keychain_store import _NativeAPI

AUTHORIZATION_TIMEOUT = 60


def authorize_once(api: _NativeAPI, services: list[str], account: str | None) -> None:
    """Select without UI, then allow consent for exactly one existing item read."""
    selected: list[tuple[str, dict[str, Any]]] = []
    for service in services:
        try:
            matches = api.call(
                "copy",
                {"service": service, **({"account": account} if account is not None else {})},
                {},
            )
        except KeychainError as exc:
            if exc.reason == "missing":
                continue
            raise
        if not isinstance(matches, list) or any(not isinstance(item, dict) for item in matches):
            raise KeychainError("unavailable")
        selected.extend((service, item) for item in matches)
    if not selected:
        raise KeychainError("missing")
    if len(selected) != 1:
        raise KeychainError("ambiguous")
    service, item = selected[0]
    reference = item.get("persistent")
    if not isinstance(reference, bytes) or not reference:
        raise KeychainError("unavailable")
    if (
        item.get("service") != service
        or not isinstance(item.get("account"), str)
        or (account is not None and item.get("account") != account)
    ):
        raise KeychainError("identity changed")
    result = api.authorize_read(reference)
    if (
        not isinstance(result, dict)
        or result.get("persistent") != reference
        or result.get("service") != service
        or result.get("account") != item.get("account")
    ):
        raise KeychainError("identity changed")
    if not isinstance(result.get("data"), bytes) or not result["data"]:
        raise KeychainError("unavailable")


def _timed_out() -> None:
    print("Claude Code Keychain: authorization timed out", file=sys.stderr, flush=True)
    os._exit(1)  # A stalled native call must not leave an unattended consent requester.


def authorization_command(argv: list[str]) -> bool:
    """Exit after explicit consent, without desktop startup or background renewal."""
    if not argv or argv[0] != "--authorize-claude-keychain":
        return False
    parser = argparse.ArgumentParser(description="Authorize one Claude Code Keychain read on macOS")
    parser.add_argument("--authorize-claude-keychain", action="store_true", required=True)
    parser.parse_args(argv)
    if sys.platform != "darwin":
        print("Claude Code Keychain: authorization requires macOS", file=sys.stderr)
        raise SystemExit(1)
    timer = threading.Timer(AUTHORIZATION_TIMEOUT, _timed_out)
    timer.daemon = True
    timer.start()
    try:
        directory = os.getenv("CLAUDE_CONFIG_DIR")
        services = service_candidates([Path(directory).expanduser()] if directory else [])
        authorize_once(_NativeAPI(), services, account_name())
    except KeychainError as exc:
        print(f"Claude Code Keychain: {keychain_reason(exc)}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Claude Code Keychain: authorization unavailable", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        timer.cancel()
    print(
        "Claude Code Keychain: foreground read authorized; verify background access before renewal"
    )
    raise SystemExit(0)
