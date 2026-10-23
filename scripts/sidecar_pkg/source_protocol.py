"""Device HMAC and durable, narrowing-only Fleet commands. No native operations."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import pathlib
import re
import tempfile
from typing import Any
from urllib import request

from scripts.sidecar_pkg import source_consent, transport
from scripts.sidecar_pkg.source_catalog import catalog
from scripts.sidecar_pkg.source_consent import SourcePolicy


def device_credentials(config: dict[str, Any] | None, url: str) -> tuple[str, str] | None:
    config = config or {}
    device_id, key, destination = (
        config.get(k) for k in ("device_id", "device_key", "device_api_url")
    )
    if (
        not isinstance(device_id, str)
        or re.fullmatch(r"[a-f0-9]{32}", device_id) is None
        or not isinstance(key, str)
        or not key
        or not isinstance(destination, str)
    ):
        return None
    # Exact base path as well as origin; never export a device key after repointing.
    if config.get("api_url", "").rstrip("/") != destination.rstrip("/") or not url.startswith(
        destination.rstrip("/") + "/api/v1/fleet/"
    ):
        return None
    return device_id, key


def signed_headers(
    config: dict[str, Any] | None, url: str, api_key: str, timestamp: str, purpose: str, body: bytes
) -> dict[str, str]:
    credentials = device_credentials(config, url)
    if credentials:
        device_id, key = credentials
        message = f"DEVICEv1|{device_id}|{timestamp}|{purpose}|".encode() + body
        headers = {"X-Runway-Device-Id": device_id}
    else:
        key = api_key
        message = timestamp.encode() + (b"GET:" if purpose == "config" else b"") + body
        headers = {}
    return {
        **headers,
        "X-Timestamp": timestamp,
        "X-Signature": hmac.new(key.encode(), message, hashlib.sha256).hexdigest(),
    }


class SourceClient:
    def __init__(self, config: dict[str, Any], path: pathlib.Path):
        self.path = path
        self.scope = (config.get("device_id"), config.get("device_api_url"))
        self.state: dict[str, Any] = {"commands": {}, "acknowledgments": [], "withdrawals": {}}
        self.accepted: set[str] = set()
        self._faulted = False
        try:
            info = path.stat()
            if path.is_symlink() or (
                os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
            ):
                raise ValueError("Unsafe source state")
            value = json.loads(path.read_text())
            if value.get("scope") == list(self.scope) and isinstance(value.get("state"), dict):
                self.state = value["state"]
        except FileNotFoundError:
            pass
        self.apply_runtime(config)

    def persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".source-control-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump({"scope": self.scope, "state": self.state}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def apply_runtime(self, config: dict[str, Any]) -> None:
        for source_id in catalog():
            command = self.state.get("commands", {}).get(source_id, {})
            generation = SourcePolicy._entry(config, source_id).get("generation", 0)
            paused = (
                self._faulted
                or command.get("paused") is True
                or (type(generation) is int and generation <= command.get("revoked_through", -1))
            )
            source_consent.POLICY.pause(source_id, paused)

    def payload(self, config: dict[str, Any]) -> dict[str, Any]:
        from scripts.sidecar_pkg.source_settings import report

        sources = []
        policy = SourcePolicy(config)
        status = report(config)
        for row in status["sources"]:
            if not row["compatible"] and (
                row["read"] or row["id"] not in config.get("keychain_withdrawals", {})
            ):
                continue
            source_id = row["id"]
            entry = SourcePolicy._entry(config, source_id)
            generation = entry.get("generation", 0)
            sources.append(
                {
                    "source_id": source_id,
                    "provider_id": catalog()[source_id].provider,
                    "label": row["label"],
                    "revision": row["revision"],
                    "generation": generation if type(generation) is int and generation >= 0 else 0,
                    "read": policy.ticket(source_id) is not None,
                    "state": row["state"],
                    "new": row["new"],
                    "renewal_requested": row["renewal_requested"],
                    "renewal_supported": False,
                    "last_read_at": row.get("last_read_at"),
                }
            )
        return {
            "protocol": 1,
            "sources": sources,
            "acknowledgments": self.state.get("acknowledgments", []),
        }

    def receive(self, config: dict[str, Any], response: Any) -> None:
        previous = copy.deepcopy(self.state)
        try:
            self._receive(config, response)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            self.state = previous
            self.accepted.clear()
            self._faulted = True
            self.apply_runtime(config)
            raise

    def _receive(self, config: dict[str, Any], response: Any) -> None:
        self.accepted.clear()
        if (
            not isinstance(response, dict)
            or response.get("protocol") != 1
            or not isinstance(response.get("commands"), list)
            or not isinstance(response.get("accepted"), list)
        ):
            raise ValueError("Unsupported source protocol")
        commands = response["commands"]
        if len(commands) > 256:
            raise ValueError("Invalid source commands")
        acknowledgments = []
        for command in sorted(commands, key=lambda item: item.get("id", 0)):
            source_id, number, generation, action = (
                command.get(k) for k in ("source_id", "id", "generation", "action")
            )
            if (
                source_id not in catalog()
                or type(number) is not int
                or number < 1
                or type(generation) is not int
                or action not in {"pause", "resume", "revoke", "request_setup", "dismiss"}
            ):
                raise ValueError("Invalid source command")
            stored = self.state.setdefault("commands", {}).setdefault(source_id, {})
            status = "applied"
            if number <= stored.get("id", 0):
                status = stored.get("status", "superseded")
            else:
                local = SourcePolicy(config).ticket(source_id)
                if action == "pause":
                    stored["paused"] = True
                elif action == "revoke":
                    stored["revoked_through"] = max(stored.get("revoked_through", -1), generation)
                elif action == "resume":
                    if (
                        local is None
                        or local.generation != generation
                        or generation <= stored.get("revoked_through", -1)
                    ):
                        status = "denied"
                    else:
                        stored["paused"] = False
                elif action == "request_setup":
                    stored["setup_requested"] = True
                elif action == "dismiss":
                    stored["dismissed_revision"] = catalog()[source_id].revision
                stored.update({"id": number, "status": status})
            acknowledgments.append({"id": number, "status": status})
        for control in response.get("controls", []):
            source_id = control.get("source_id")
            paused, revoked = control.get("paused"), control.get("revoked_through")
            if source_id not in catalog() or type(paused) is not bool or type(revoked) is not int:
                raise ValueError("Invalid source control")
            stored = self.state.setdefault("commands", {}).setdefault(source_id, {})
            stored["paused"] = paused
            stored["revoked_through"] = max(stored.get("revoked_through", -1), revoked)
        self.state["acknowledgments"] = acknowledgments
        for withdrawal in response.get("withdrawals", []):
            source_id, generation = withdrawal.get("source_id"), withdrawal.get("generation")
            if source_id in catalog() and type(generation) is int:
                self.state.setdefault("withdrawals", {})[source_id] = max(
                    self.state.get("withdrawals", {}).get(source_id, -1), generation
                )
        self.persist()
        self._faulted = False
        self.apply_runtime(config)
        self.accepted = {
            source_id
            for source_id in response["accepted"]
            if isinstance(source_id, str) and source_id in catalog()
        }

    def sync(self, config: dict[str, Any]) -> bool:
        self.accepted.clear()
        self.apply_runtime(config)
        url = str(config.get("api_url", "")).rstrip("/") + "/api/v1/fleet/source-sync"
        if device_credentials(config, url) is None:
            return False
        from scripts.sidecar_pkg.tls import build_context_from_config

        body = json.dumps(self.payload(config), separators=(",", ":")).encode()
        headers = signed_headers(
            config, url, "", transport.signing_timestamp(), "source-sync", body
        )
        req = request.Request(
            url, body, headers={**headers, "Content-Type": "application/json"}, method="POST"
        )
        try:
            with transport.urlopen(
                req, timeout=10, context=build_context_from_config(url, config)
            ) as response:
                if response.geturl() != url:
                    return False
                self.receive(config, json.loads(response.read(262145)))
            return True
        except (OSError, ValueError, TypeError, AttributeError):
            self.accepted.clear()
            return False


CLIENT: SourceClient | None = None


def prepare(config: dict[str, Any], path: pathlib.Path) -> SourceClient | None:
    """Load durable narrowing controls before any background renewer can start."""
    global CLIENT
    url = str(config.get("api_url", "")).rstrip("/") + "/api/v1/fleet/source-sync"
    if device_credentials(config, url) is None:
        CLIENT = None
        return None
    try:
        scope = (config.get("device_id"), config.get("device_api_url"))
        if CLIENT is None or CLIENT.scope != scope or CLIENT.path != path:
            CLIENT = SourceClient(config, path)
        CLIENT.apply_runtime(config)
        return CLIENT
    except (OSError, ValueError, TypeError, AttributeError):
        CLIENT = None
        for source_id in catalog():
            source_consent.POLICY.pause(source_id, True)
        return None


def sync(config: dict[str, Any], path: pathlib.Path) -> bool:
    client = prepare(config, path)
    return client.sync(config) if client is not None else False


def forwarding_allowed(source_id: str, destination: str) -> bool:
    return bool(CLIENT and CLIENT.scope[1] == destination and source_id in CLIENT.accepted)
