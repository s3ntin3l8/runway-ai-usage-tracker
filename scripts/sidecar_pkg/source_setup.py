"""Explicit foreground setup; background reporting never invokes this controller."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from scripts.sidecar_pkg import source_consent as consent
from scripts.sidecar_pkg.source_catalog import catalog


class _LocalRefusedError(Exception):
    """The local configuration does not authorize this source.

    The reason is a fixed phrase rather than the underlying error, so nothing
    about the local configuration can reach the output.
    """


def resources(source_id: str) -> tuple[list[str], str | None]:
    if source_id == "anthropic.claude-code":
        from scripts.sidecar import local_keychain_login_dirs
        from scripts.sidecar_pkg.claude_code_keychain import account_name, service_candidates

        return service_candidates(local_keychain_login_dirs()), account_name()
    descriptor = catalog()[source_id]
    return list(descriptor.services), descriptor.account


def launch_authorization(source_id: str) -> bool:
    if not getattr(sys, "frozen", False) or sys.platform != "darwin":
        return False
    ticket = consent.POLICY.ticket(source_id)
    if ticket is None:
        return False
    try:
        with subprocess.Popen(
            [sys.executable, "--authorize-keychain-source", source_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as child:
            deadline = time.monotonic() + 65
            while child.poll() is None:
                if not consent.POLICY.current(ticket) or time.monotonic() >= deadline:
                    child.kill()
                    child.wait(timeout=2)
                    return False
                try:
                    child.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
            return child.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def verify_background(source_id: str, ticket: consent.ConsentTicket) -> bool:
    from scripts.sidecar_pkg.keychain_client import KeychainClient
    from scripts.sidecar_pkg.keychain_protocol import KeychainError

    services, account = resources(source_id)
    client = KeychainClient()
    deadline = time.monotonic() + 30

    def bounded_call(query: dict) -> object:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise KeychainError("timed_out")
        client._timeout = min(client._timeout, remaining)
        return client.call(
            "copy", query, {}, group=source_id, allowed=lambda: consent.POLICY.current(ticket)
        )

    try:
        selected = []
        for service in services:
            query = {"service": service, **({"account": account} if account is not None else {})}
            try:
                matches = bounded_call(query)
            except KeychainError as exc:
                if exc.reason == "missing":
                    continue
                raise
            if not isinstance(matches, list):
                return False
            selected.extend(
                item
                for item in matches
                if isinstance(item, dict)
                and item.get("service") == service
                and (account is None or item.get("account") == account)
            )
        if len(selected) != 1:
            return False
        item = selected[0]
        value = bounded_call({"persistent": item["persistent"]})
        return (
            isinstance(value, dict)
            and value.get("persistent") == item["persistent"]
            and value.get("service") == item["service"]
            and value.get("account") == item.get("account")
            and isinstance(value.get("data"), bytes)
            and bool(value["data"])
        )
    except (KeychainError, KeyError, TypeError):
        return False
    finally:
        client.close()


class SetupController:
    def __init__(
        self,
        *,
        launch: Callable[[str], bool] = launch_authorization,
        verify: Callable[[str, consent.ConsentTicket], bool] = verify_background,
    ) -> None:
        self._launch, self._verify = launch, verify
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._states: dict[str, tuple[consent.ConsentTicket, str]] = {}

    def state(self, source_id: str) -> str:
        with self._lock:
            ticket = consent.POLICY.ticket(source_id)
            if ticket is None:
                return "Off"
            previous = self._states.get(source_id)
            return previous[1] if previous and previous[0] == ticket else "Setup required"

    def start(self, source_id: str) -> str:
        with self._lock:
            ticket = consent.POLICY.ticket(source_id)
            if ticket is None:
                return "Off"
            if self._thread is not None and self._thread.is_alive():
                return "Connecting"
            self._states[source_id] = (ticket, "Connecting")
            self._thread = threading.Thread(
                target=self._run, args=(source_id, ticket), name="KeychainSetup", daemon=True
            )
            self._thread.start()
            return "Connecting"

    def _run(self, source_id: str, ticket: consent.ConsentTicket) -> None:
        success = False
        try:
            success = (
                consent.POLICY.current(ticket)
                and self._launch(source_id)
                and consent.POLICY.current(ticket)
                and self._verify(source_id, ticket)
            )
        except Exception:
            pass  # Never expose native/subprocess diagnostic text.
        with self._lock:
            if success and consent.POLICY.current(ticket):
                from scripts.sidecar_pkg import macos_cookies
                from scripts.sidecar_pkg.keychain_client import get_keychain_client

                client = get_keychain_client()
                if source_id == "anthropic.claude-code":
                    client.clear_access("claude-code")
                elif source_id == "github.gh-cli":
                    client.clear_access("github-cli")
                else:
                    for service in catalog()[source_id].services:
                        client.clear_access("storage:" + service)
                        with macos_cookies._KEY_LOCK:
                            macos_cookies._KEY_CACHE.pop(service, None)
                            macos_cookies._KEY_STATUS.pop(service, None)
            if consent.POLICY.current(ticket):
                self._states[source_id] = (ticket, "Limited" if success else "Action required")

    def wait(self, timeout: float) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)


SETUP = SetupController()


def authorization_command(argv: list[str]) -> None:
    """Packaged child entrypoint, reachable only through explicit local setup."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-keychain-source", choices=tuple(catalog()), required=True)
    args = parser.parse_args(argv)
    source = args.authorize_keychain_source
    if not getattr(sys, "frozen", False) or sys.platform != "darwin":
        print(f"{source}: authorization requires macOS", file=sys.stderr)
        raise SystemExit(1)
    timer = threading.Timer(60, lambda: os._exit(1))
    timer.daemon = True
    timer.start()
    try:
        from scripts.sidecar_pkg.keychain_authorization import authorize_once
        from scripts.sidecar_pkg.macos_keychain_store import _NativeAPI
        from sidecar_app.config import get_config_path

        config_path = get_config_path()
        if (
            config_path.is_symlink()
            or config_path.stat().st_uid != os.getuid()
            or config_path.stat().st_mode & 0o077
        ):
            raise _LocalRefusedError("local permissions unavailable")
        config = json.loads(config_path.read_text())
        if (
            not isinstance(config, dict)
            or consent.SourcePolicy(config).ticket(args.authorize_keychain_source) is None
        ):
            raise _LocalRefusedError("local permission disabled")
        from scripts.sidecar import configure_login_dirs

        configure_login_dirs(config)
        services, account = resources(args.authorize_keychain_source)
        authorize_once(_NativeAPI(), services, account)
    except _LocalRefusedError as exc:
        # The reason is a fixed phrase, never the underlying error, so nothing
        # about the local configuration can reach the output.
        print(f"{source}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(f"{source}: authorization unavailable", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        timer.cancel()
    raise SystemExit(0)
