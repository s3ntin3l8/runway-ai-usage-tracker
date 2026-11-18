"""Device HMAC and durable, narrowing-only Fleet commands. No native operations."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import sys
import tempfile
from collections.abc import Callable
from typing import Any
from urllib import error, request

from scripts.sidecar_pkg import source_consent, transport
from scripts.sidecar_pkg.browser_store import SOURCE_BLOCK_REASONS, SOURCE_REPORT_KEYS
from scripts.sidecar_pkg.source_catalog import catalog, control_catalog, source_for_origin
from scripts.sidecar_pkg.source_consent import SourcePolicy

logger = logging.getLogger(__name__)


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
        self.re_pair_required = False
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

    def payload(self, config: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
        sources = []
        policy = SourcePolicy(config)
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
                source_id not in control_catalog()
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
                    ordinary_source = (
                        sys.platform in {"linux", "win32"}
                        and source_id.rsplit(".", 1)[-1] in {"chrome", "chromium", "edge"}
                    ) or (
                        sys.platform == "win32"
                        and control_catalog()[source_id].platforms == ("win32",)
                    )
                    local_generation = (
                        local.generation
                        if local is not None
                        else generation
                        if ordinary_source
                        else None
                    )
                    if local_generation != generation or generation <= stored.get(
                        "revoked_through", -1
                    ):
                        status = "denied"
                    else:
                        stored["paused"] = False
                elif action == "request_setup":
                    stored["setup_requested"] = True
                elif action == "dismiss":
                    stored["dismissed_revision"] = control_catalog()[source_id].revision
                stored.update({"id": number, "status": status})
            acknowledgments.append({"id": number, "status": status})
        for control in response.get("controls", []):
            source_id = control.get("source_id")
            paused, revoked = control.get("paused"), control.get("revoked_through")
            if (
                source_id not in control_catalog()
                or type(paused) is not bool
                or type(revoked) is not int
            ):
                raise ValueError("Invalid source control")
            stored = self.state.setdefault("commands", {}).setdefault(source_id, {})
            stored["paused"] = paused
            stored["revoked_through"] = max(stored.get("revoked_through", -1), revoked)
        self.state["acknowledgments"] = acknowledgments
        for withdrawal in response.get("withdrawals", []):
            source_id, generation = withdrawal.get("source_id"), withdrawal.get("generation")
            if source_id in control_catalog() and type(generation) is int:
                self.state.setdefault("withdrawals", {})[source_id] = max(
                    self.state.get("withdrawals", {}).get(source_id, -1), generation
                )
        self.persist()
        self._faulted = False
        self.re_pair_required = False
        self.apply_runtime(config)
        self.accepted = {
            source_id
            for source_id in response["accepted"]
            if isinstance(source_id, str) and source_id in control_catalog()
        }

    def sync(
        self, config: dict[str, Any], report: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> bool:
        self.accepted.clear()
        self.apply_runtime(config)
        url = str(config.get("api_url", "")).rstrip("/") + "/api/v1/fleet/source-sync"
        if device_credentials(config, url) is None:
            return False
        from scripts.sidecar_pkg.tls import build_context_from_config

        body = json.dumps(self.payload(config, report(config)), separators=(",", ":")).encode()
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
        except error.HTTPError as exc:
            if exc.code == 401:
                if not self.re_pair_required:
                    logger.warning(
                        "Device authentication rejected; re-pair this sidecar with the server"
                    )
                self.re_pair_required = True
            self.accepted.clear()
            return False
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


def sync(
    config: dict[str, Any],
    path: pathlib.Path,
    report: Callable[[dict[str, Any]], dict[str, Any]],
) -> bool:
    client = prepare(config, path)
    return client.sync(config, report) if client is not None else False


def forwarding_allowed(source_id: str, destination: str) -> bool:
    return bool(CLIENT and CLIENT.scope[1] == destination and source_id in CLIENT.accepted)


def filter_egress(data: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recheck local consent and negotiated withdrawal support immediately at egress."""
    config = config or {}
    destination = str(config.get("api_url", ""))
    authenticated = (
        device_credentials(config, destination.rstrip("/") + "/api/v1/fleet/ingest") is not None
    )

    def sanitize(value: Any, provider_override: Any = None, *, source_report: bool = False) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        provider, origin = (
            str(provider_override or value.get("provider_id", "")),
            str(value.get("credential_origin", "")),
        )
        source_id = source_for_origin(provider, origin)
        proof = value.get("keychain_consent")
        # A report about a credential source the machine could not read is not
        # keychain-bound: it names the store, gives a reason from a fixed
        # vocabulary and states the remedy - no keychain-derived material, no
        # account id, no secret. The consent rules gate what is read out of the
        # Keychain, so they do not apply to it. Without this exemption the report
        # is dropped on macOS - where every browser-cookie origin counts as a
        # native-browser source - and the provider looks like nobody is signed
        # into it.
        #
        # The shape is the proof, not the reason alone: only a manifest entry
        # carries the report's own keys, and only those keys pass. A dict that
        # happens to carry the same reason - a metric's metadata, an
        # observation, an entry with something else attached - is still gated.
        # The key set is the whole guarantee, and it fails closed: a new key on
        # a report stops it from being one.
        source_block = (
            source_report
            and str(value.get("reason", "")) in SOURCE_BLOCK_REASONS
            and set(value) <= SOURCE_REPORT_KEYS
        )
        keychain = not source_block and (
            "keychain_consent" in value or (sys.platform == "darwin" and bool(source_id))
        )
        if keychain:
            if (
                not authenticated
                or not isinstance(proof, dict)
                or not source_consent.valid_stamp(proof)
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
            # Only a manifest entry can be a source report.
            result[field] = [
                clean
                for item in data[field]
                if (clean := sanitize(item, source_report=field == "entries")) is not None
            ]
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
