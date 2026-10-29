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
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg import source_consent as consent
from scripts.sidecar_pkg.keychain_protocol import KeychainError, keychain_reason
from scripts.sidecar_pkg.source_catalog import catalog


class _LocalRefusedError(Exception):
    """The local configuration does not authorize this source.

    The reason is a fixed phrase rather than the underlying error, so nothing
    about the local configuration can reach the output.
    """


def _attach_parent_console() -> int | None:
    """Attach to the console the user launched from, if there is one.

    A windowed (console=False) build has no stdout and no stderr, so a
    printed diagnostic is invisible exactly where it is needed. Attaching to
    the parent console is the standard fix; it fails harmlessly when there is
    no console to attach to.

    Returns the console's previous output code page so the caller can put it
    back: the code page is process-wide, and leaving the user's console on
    UTF-8 changes what every later command prints.
    """
    if sys.platform != "win32" or sys.stderr is not None:
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return None
        previous_code_page = int(kernel32.GetConsoleOutputCP())
        kernel32.SetConsoleOutputCP(65001)
        try:
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        except Exception:
            _restore_console_code_page(previous_code_page)
            raise
        return previous_code_page
    except Exception:
        # A console that cannot be attached to is not a reason to fail the
        # refusal: the caller falls back to writing it to a file.
        return None


def _restore_console_code_page(previous: int | None) -> None:
    """Put the console's output code page back the way the user had it."""
    if previous is None:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(previous)
    except Exception:
        # Nothing to restore to; the console keeps whatever it had.
        pass


def _write_refusal_file(source: str, reason: str) -> Path | None:
    """Write the refusal next to the sidecar's config, for a build with no console.

    Returns the path it wrote, or None when the write failed - a caller must
    not point at a report that does not exist.
    """
    try:
        from sidecar_app.config import get_config_path

        path = get_config_path().parent / "source_authorization.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{source}: {reason}\n", encoding="utf-8")
    except (OSError, Exception):
        return None
    return path


def _notify_windows(message: str) -> None:
    """Show a message box; the only way a windowed build can talk to its user."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Runway Sidecar", 0x40)
    except Exception:
        # No message box available: the report file is the fallback.
        pass


def _refuse(source: str, reason: str) -> None:
    """Say why the authorization was refused, wherever the user can look.

    A windowed build has no stdout and no stderr, so a printed message is
    invisible exactly where it is needed. The message goes to the console when
    there is one and to a report file otherwise, the way the MiniMax Code
    probe reports.
    """
    previous_code_page = _attach_parent_console()
    if sys.stderr is not None:
        try:
            print(f"{source}: {reason}", file=sys.stderr)
        finally:
            try:
                sys.stderr.flush()
            except Exception:
                # A stream that cannot be flushed has nothing left to decode.
                pass
            _restore_console_code_page(previous_code_page)
        return
    path = _write_refusal_file(source, reason)
    if path is None:
        _notify_windows(f"{source}: {reason}")
        return
    _notify_windows(f"{source}: {reason}\n\nThe reason was written to:\n{path}")


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
        # The running (or last) authorize-all: groups of sources behind one Keychain item.
        self._batch: dict[str, Any] | None = None

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
        self._finish(source_id, ticket, success)

    def _finish(self, source_id: str, ticket: consent.ConsentTicket, success: bool) -> None:
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

    def start_all(self, source_ids: list[str]) -> dict[str, Any]:
        """Authorize every given source that is on and not authorized yet, one Keychain item at a time.

        Sources that read the same Keychain item are grouped, so the child that asks macOS for
        consent runs once per item and each source behind it is then verified on its own. It is
        single-flight with ``start``: while a run is active this changes nothing and reports it.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.run_report()
            descriptors = catalog()
            groups: dict[str, list[tuple[str, consent.ConsentTicket]]] = {}
            for source_id in source_ids:
                descriptor = descriptors.get(source_id)
                ticket = consent.POLICY.ticket(source_id)
                if descriptor is None or ticket is None or self.state(source_id) == "Limited":
                    continue
                groups.setdefault(descriptor.item_key, []).append((source_id, ticket))
            if not groups:
                self._batch = None
                return self.run_report()
            self._batch = {
                "groups": list(groups.values()),
                "index": 0,
                "done": 0,
                "cancelled": False,
            }
            self._thread = threading.Thread(
                target=self._run_batch, name="KeychainSetupBatch", daemon=True
            )
            self._thread.start()
            return self.run_report()

    def _run_batch(self) -> None:
        batch = self._batch
        if batch is None:
            return
        for index, members in enumerate(batch["groups"]):
            with self._lock:
                if batch["cancelled"]:
                    break
                batch["index"] = index
                # A source turned off since the run started is skipped, not authorized.
                live = [(i, t) for i, t in members if consent.POLICY.current(t)]
                for source_id, ticket in live:
                    self._states[source_id] = (ticket, "Connecting")
            if not live:
                batch["done"] += 1
                continue
            launched = False
            try:
                launched = self._launch(live[0][0])
            except Exception:
                pass  # Never expose native/subprocess diagnostic text.
            for source_id, ticket in live:
                success = False
                try:
                    success = (
                        launched
                        and consent.POLICY.current(ticket)
                        and self._verify(source_id, ticket)
                    )
                except Exception:
                    pass
                self._finish(source_id, ticket, success)
            with self._lock:
                batch["done"] += 1
        with self._lock:
            batch["finished"] = True

    def cancel(self) -> None:
        """Stop after the Keychain item being authorized now; sources still queued stay as they were."""
        with self._lock:
            if self._batch is not None:
                self._batch["cancelled"] = True

    def run_report(self) -> dict[str, Any]:
        """Secret-free progress of the authorize-all run, for the settings page."""
        with self._lock:
            batch = self._batch
            if batch is None:
                return {
                    "active": False,
                    "total": 0,
                    "done": 0,
                    "current": [],
                    "queued": [],
                    "cancelled": False,
                }
            groups, index = batch["groups"], batch["index"]
            active = not batch.get("finished", False)
            cancelled = batch["cancelled"]
            return {
                "active": active,
                "total": len(groups),
                "done": batch["done"],
                "current": [i for i, _ in groups[index]] if active else [],
                "queued": []
                if cancelled or not active
                else [i for g in groups[index + 1 :] for i, _ in g],
                "cancelled": cancelled,
            }

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
    if sys.platform != "darwin":
        _refuse(source, "authorization requires macOS")
        raise SystemExit(1)
    if not getattr(sys, "frozen", False):
        _refuse(source, "authorization requires the packaged build")
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
    except KeychainError as exc:
        # The reason is whitelisted, never the underlying error, so nothing
        # about the keychain can reach the output.
        _refuse(source, keychain_reason(exc))
        raise SystemExit(1) from None
    except _LocalRefusedError as exc:
        # The reason is a fixed phrase, never the underlying error, so nothing
        # about the local configuration can reach the output.
        _refuse(source, str(exc))
        raise SystemExit(1) from None
    except Exception:
        _refuse(source, "authorization unavailable")
        raise SystemExit(1) from None
    finally:
        timer.cancel()
    raise SystemExit(0)
