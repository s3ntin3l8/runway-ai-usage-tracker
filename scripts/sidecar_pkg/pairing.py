"""Sidecar half of one-time pairing (``runway-sidecar://pair`` deep links).

The Runway dashboard mints a short-lived code and hands the user a link like
``runway-sidecar://pair?server=https%3A%2F%2Frunway.example.com&code=AB3DE-7XYZ9``.
This module parses such a link (or a server + code typed by hand), redeems the
code at ``POST <server>/api/v1/fleet/pair`` over TLS, and returns the
``api_url`` / ``api_key`` to write into ``config.json``. The ingest key never
appears in the link itself.

Security posture — a deep link is attacker-reachable input (any web page can
fire one), and pairing re-points where this machine ships provider tokens:

* Callers MUST get explicit user confirmation that names the target server
  before calling :func:`redeem` (the tray's settings page does; the CLI is an
  explicit command).
* ``https`` is required, except for loopback servers.
* URLs with credentials, fragments or odd schemes are rejected outright.

Stdlib only (plus the shared ``tls`` helper); used by both the tray app and the
headless CLI (``runway-sidecar-cli --pair``).
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from dataclasses import dataclass
from urllib import error, request
from urllib.parse import parse_qs, urlsplit

SCHEME = "runway-sidecar"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_TIMEOUT_SECONDS = 20


class PairingError(Exception):
    """A pairing link/code that can't be used; message is user-presentable."""


@dataclass(frozen=True)
class PairTarget:
    server: str  # normalized scheme://host[:port][/path]
    code: str

    @property
    def is_loopback(self) -> bool:
        return (urlsplit(self.server).hostname or "") in _LOOPBACK


def normalize_server(server: str) -> str:
    """Validate a server base URL; raise ``PairingError`` if unusable."""
    parts = urlsplit((server or "").strip())
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        raise PairingError("The server address must start with https://")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise PairingError("The server address must not contain credentials or parameters")
    if parts.scheme == "http" and host not in _LOOPBACK:
        raise PairingError(
            "Refusing to pair over plain http:// with a remote server; use its https:// address"
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def normalize_code(code: str) -> str:
    cleaned = "".join(ch for ch in (code or "") if ch.isalnum() or ch == "-").strip("-")
    if not 8 <= len(cleaned.replace("-", "")) <= 16:
        raise PairingError("That pairing code doesn't look right")
    return cleaned.upper()


def is_pair_url(value: str | None) -> bool:
    return bool(value) and str(value).lower().startswith(f"{SCHEME}:")


def parse_pair_url(url: str) -> PairTarget:
    """Parse ``runway-sidecar://pair?server=…&code=…`` into a validated target."""
    parts = urlsplit((url or "").strip())
    if parts.scheme.lower() != SCHEME:
        raise PairingError("Not a Runway pairing link")
    # Accept both runway-sidecar://pair?… and runway-sidecar:pair?… forms.
    action = (parts.netloc or parts.path).strip("/").lower()
    if action != "pair":
        raise PairingError("Unsupported Runway link")
    q = parse_qs(parts.query)
    server = (q.get("server") or [""])[0]
    code = (q.get("code") or [""])[0]
    return PairTarget(server=normalize_server(server), code=normalize_code(code))


def redeem(target: PairTarget, *, hostname: str | None = None) -> dict[str, str]:
    """Exchange the code for ``{"api_url", "api_key"}``. Raises ``PairingError``."""
    from scripts.sidecar_pkg.tls import build_context

    url = f"{target.server}/api/v1/fleet/pair"
    body = json.dumps({"code": target.code, "hostname": hostname}).encode()
    req = request.Request(  # noqa: S310 — scheme validated by normalize_server
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "Runway-Sidecar-Pairing"},
    )
    try:
        with request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=build_context(url)) as resp:  # noqa: S310
            data = json.loads(resp.read().decode())
    except error.HTTPError as exc:
        if exc.code == 400:
            raise PairingError("The pairing code is invalid, expired or already used") from exc
        if exc.code == 429:
            raise PairingError("Too many attempts; wait a minute and try again") from exc
        if exc.code == 503:
            raise PairingError("The server has sidecar ingest disabled (INGEST_API_KEY)") from exc
        raise PairingError(f"The server answered HTTP {exc.code}") from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise PairingError(f"Could not reach {target.server}: {reason}") from exc
    except ValueError as exc:
        raise PairingError("The server sent an unexpected response") from exc

    api_url = str(data.get("api_url") or "").strip()
    api_key = str(data.get("api_key") or "").strip()
    if not api_url or not api_key:
        raise PairingError("The server sent an incomplete response")
    return {"api_url": normalize_server(api_url), "api_key": api_key}


def write_config(config_path: pathlib.Path, api_url: str, api_key: str) -> None:
    """Merge the paired credentials into *config_path* atomically (mode 0600)."""
    config_path = pathlib.Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    current["api_url"] = api_url
    current["api_key"] = api_key
    fd, tmp = tempfile.mkstemp(prefix=".config-", dir=str(config_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, config_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            # Temp file already gone; the original error below is what matters.
            pass
        raise
