#!/usr/bin/env python3
"""
Runway Sidecar (Generated) - Token and Local Data Collector

Architecture:
- Data-driven collection based on a central registry.
- Extracts tokens/cookies from local files, keychain, and Credential Manager.
- Reads local data files (SQLite DBs, JSON logs).
- Sends data to Runway server via /api/v1/fleet/ingest.

IMPORTANT: This sidecar does NOT make API calls directly.
All API calls are done by the server using tokens we provide.
"""

import argparse
import atexit
import ctypes
import datetime
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import signal
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib import error, request

# When invoked as `python scripts/sidecar.py`, Python sets sys.path[0] to
# scripts/, so `from scripts.sidecar_pkg.*` (used for event extractor lazy
# imports below) cannot resolve. Prepend the repo root so the package is found.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _resolve_sidecar_version() -> str:
    """Source-of-truth for the sidecar version reported to the server.

    Looks for `package.json` next to the running code: under
    `sys._MEIPASS` for PyInstaller-frozen binaries (the spec files bundle
    it as a data file), and at the repo root when running from source.
    Falls back to the baked constant only if neither path resolves.

    Frozen binaries report exactly what the build stamped — release binaries
    carry a plain `X.Y.Z`; the edge workflow stamps `X.Y.Z+edge.<sha>`. A
    from-source run instead self-classifies from its git position: `main` is
    the edge line and `vX.Y.Z` tags are positions on it, so a checkout sitting
    past the latest release tag is running edge code and gets the same
    `+edge.<sha>` stamp an edge binary would (see `_from_source_edge_suffix`).
    """
    base = _SIDECAR_VERSION_FALLBACK
    meipass = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if meipass:
        candidates.append(Path(meipass) / "package.json")
    candidates.append(_REPO_ROOT / "package.json")
    for pkg_json in candidates:
        if pkg_json.is_file():
            try:
                with open(pkg_json) as _f:
                    version = json.load(_f).get("version")
            except (OSError, ValueError):
                continue
            if isinstance(version, str) and version:
                base = version
                break
    if meipass:
        # Frozen build — trust the stamp baked in at build time.
        return base
    return base + _from_source_edge_suffix()


def _from_source_edge_suffix() -> str:
    """`+edge.<sha>` when this source checkout is past the latest release tag.

    Trunk-based model: `main` is the edge line and `vX.Y.Z` tags are positions
    on it. A from-source run sitting exactly on a clean release tag is that
    release (stable); anything ahead of it — or untagged, or dirty — is edge
    code, mirroring what the edge build stamps. Returns '' when git is
    unavailable or the position can't be determined, so we never mislabel.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(_REPO_ROOT),
                "describe",
                "--tags",
                "--match",
                "v*",
                "--long",
                "--dirty",
                "--always",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    desc = result.stdout.strip()
    # 'vX.Y.Z-<ahead>-g<sha>' (+ optional '-dirty'); ahead==0 and clean ⇒ on the tag.
    m = re.match(r"^v.+-(\d+)-g([0-9a-f]+)(-dirty)?$", desc)
    if m:
        ahead, sha, dirty = int(m.group(1)), m.group(2), m.group(3)
        return "" if (ahead == 0 and not dirty) else f"+edge.{sha}"
    # No reachable v* tag: bare '<sha>' (+ optional '-dirty') ⇒ edge/dev checkout.
    m = re.match(r"^([0-9a-f]{7,})(?:-dirty)?$", desc)
    if m:
        return f"+edge.{m.group(1)[:12]}"
    return ""


def _frozen_runtime_missing() -> bool:
    """True when this is a frozen (PyInstaller onefile) process whose extraction
    directory has been deleted out from under it — e.g. by an external `/tmp`
    sweep or cleanup job racing a long-running daemon.

    This is a precise, local corruption check: it doesn't fire on ordinary
    network failures (server unreachable, 5xx, timeouts), only when the
    running binary's own unpacked runtime is gone. `Restart=always` in the
    systemd unit re-extracts a fresh copy on the next launch, so treating this
    as fatal is the fix — the alternative is looping forever against a runtime
    that can never recover on its own (see `http_post_signed`'s errno-2
    failures once bundled files like certifi's CA bundle vanish).
    """
    meipass = getattr(sys, "_MEIPASS", None)
    return bool(meipass) and not os.path.isdir(meipass)


_SIDECAR_VERSION_FALLBACK = (
    "0.13.0"  # last-resort default; release flow keeps package.json authoritative
)
_SIDECAR_VERSION = _resolve_sidecar_version()

# --- INJECTED REGISTRY ---
__REGISTRY__: dict[str, Any] = {
    "providers": {
        "anthropic": {
            "name": "Claude Pro",
            "icon": "\ud83d\udfe0",
            "rules": [
                {
                    "type": "env",
                    "variable": "CLAUDE_CODE_OAUTH_TOKEN",
                    "mapping": {"value": "oauth_token"},
                },
                {
                    "type": "file",
                    "paths": [
                        "~/.claude/.credentials.json",
                        "{{CONFIG_DIR:claude}}/.credentials.json",
                    ],
                    "format": "json",
                    "mapping": {
                        "claudeAiOauth.accessToken": "oauth_token",
                        "claudeAiOauth.refreshToken": "refresh_token",
                        "claudeAiOauth.clientId": "client_id",
                    },
                },
                {
                    "type": "keychain",
                    "service_name": "Claude Code-credentials",
                    "format": "json",
                    "mapping": {
                        "claudeAiOauth.accessToken": "oauth_token",
                        "claudeAiOauth.refreshToken": "refresh_token",
                        "claudeAiOauth.clientId": "client_id",
                    },
                },
                {
                    "type": "cookie",
                    "domains": ["anthropic.com", ".anthropic.com", "claude.ai", ".claude.ai"],
                    "name": "sessionKey",
                    "mapping": {"value": "cookie_sessionKey"},
                },
                {
                    "type": "file_json_statusline",
                    "paths": ["~/.claude/statusline.json", "{{CONFIG_DIR:claude}}/statusline.json"],
                },
            ],
        },
        "openrouter": {
            "name": "OpenRouter",
            "icon": "\ud83d\ude80",
            "rules": [
                {"type": "env", "variable": "OPENROUTER_API_KEY", "mapping": {"value": "api_key"}},
                {
                    "type": "env",
                    "variable": "OPENROUTER_HTTP_REFERER",
                    "mapping": {"value": "http_referer"},
                },
                {"type": "env", "variable": "OPENROUTER_X_TITLE", "mapping": {"value": "x_title"}},
            ],
        },
        "minimax": {
            "name": "MiniMax",
            "icon": "\ud83e\udd16",
            "rules": [
                {"type": "env", "variable": "MINIMAX_API_KEY", "mapping": {"value": "api_key"}}
            ],
        },
        "github": {
            "name": "GitHub Copilot",
            "icon": "\ud83d\udc19",
            "rules": [
                {"type": "env", "variable": "GITHUB_TOKEN", "mapping": {"value": "api_key"}},
                {
                    "type": "file",
                    "paths": ["{{CONFIG_DIR:runway}}/github_oauth.json"],
                    "format": "json",
                    "mapping": {"access_token": "api_key"},
                },
                {
                    "type": "file",
                    "paths": [
                        "~/.config/gh/hosts.yml",
                        "{{CONFIG_DIR:gh}}/hosts.yml",
                        "{{CONFIG_DIR:GitHub CLI}}/hosts.yml",  # Windows: %APPDATA%\GitHub CLI\
                    ],
                    "format": "yaml",
                    "mapping": {"oauth_token": "api_key"},
                },
                {
                    "type": "windows_credential",
                    "target": "github.com",
                    "mapping": {"value": "api_key"},
                },
                {
                    "type": "exec",
                    "command": ["git", "config", "--global", "user.email"],
                    "mapping": {"value": "name"},
                },
            ],
        },
        "gemini": {
            "name": "Gemini API",
            "icon": "🔵",
            "rules": [
                {
                    "type": "file",
                    "paths": [
                        "~/.gemini/oauth_creds.json",
                        "{{CONFIG_DIR:gemini}}/oauth_creds.json",
                    ],
                    "format": "json",
                    "mapping": {
                        "access_token": "oauth_token",
                        "refresh_token": "refresh_token",
                        # The Google account email lives inside the id_token JWT,
                        # not as a top-level field. Ship it so the server can
                        # derive the canonical (email) account_id instead of
                        # falling back to "default".
                        "id_token": "id_token",
                        # Access-token expiry (ms epoch). Opaque ya29.* tokens carry
                        # no JWT exp, so this is the freshness signal that stops a
                        # stale local token from clobbering a server-refreshed one.
                        "expiry_date": "expiry_date",
                        "client_id": "client_id",
                        "clientId": "client_id",
                    },
                },
            ],
        },
        "chatgpt": {
            "name": "ChatGPT Codex",
            "icon": "💬",
            "rules": [
                {
                    "type": "cookie",
                    "domains": ["chatgpt.com"],
                    "name": "__Secure-next-auth.session-token",
                    "mapping": {"value": "cookie___Secure-next-auth.session-token"},
                },
                # NextAuth.js splits the session token into .0 / .1 chunks when it
                # exceeds the 4 KB cookie size limit. Collect both so the server can
                # reassemble them before the /api/auth/session exchange.
                {
                    "type": "cookie",
                    "domains": ["chatgpt.com"],
                    "name": "__Secure-next-auth.session-token.0",
                    "mapping": {"value": "cookie___Secure-next-auth.session-token.0"},
                },
                {
                    "type": "cookie",
                    "domains": ["chatgpt.com"],
                    "name": "__Secure-next-auth.session-token.1",
                    "mapping": {"value": "cookie___Secure-next-auth.session-token.1"},
                },
                # OpenAI service-credential cookie — required by the /api/auth/session
                # token-exchange endpoint alongside the session token.
                {
                    "type": "cookie",
                    "domains": ["chatgpt.com"],
                    "name": "oai-sc",
                    "mapping": {"value": "cookie_oai-sc"},
                },
                {
                    "type": "file",
                    "paths": ["~/.codex/auth.json", "{{CONFIG_DIR:codex}}/auth.json"],
                    "format": "json",
                    "mapping": {
                        "tokens.access_token": "oauth_token",
                        "tokens.refresh_token": "refresh_token",
                        "tokens.id_token": "id_token",
                        "tokens.account_id": "account_id",
                    },
                },
                {
                    "type": "env",
                    "variable": "CHATGPT_OAUTH_TOKEN",
                    "mapping": {"value": "oauth_token"},
                },
            ],
        },
        "kimi": {
            "name": "Kimi API",
            "icon": "\ud83c\udf19",
            "rules": [
                {
                    "type": "env",
                    "variable": "KIMI_AUTH_TOKEN",
                    "mapping": {"value": "cookie_kimi-auth"},
                },
                {"type": "env", "variable": "KIMI_API_KEY", "mapping": {"value": "api_key"}},
                {
                    "type": "cookie",
                    "domains": ["kimi.moonshot.cn", "kimi.com"],
                    "name": "kimi-auth",
                    "mapping": {"value": "cookie_kimi-auth"},
                },
            ],
        },
        "kimi_k2": {
            "name": "Kimi K2",
            "icon": "🌙",
            "rules": [
                {"type": "env", "variable": "KIMI_K2_API_KEY", "mapping": {"value": "api_key"}},
                {
                    "type": "file",
                    "paths": ["~/.kimi/config.json", "~/.k2/tokens.json"],
                    "format": "json",
                    "mapping": {"api_key": "api_key", "token": "api_key"},
                },
            ],
        },
        "kimi_coding": {
            "name": "Kimi Coding",
            "icon": "🌙",
            "rules": [
                {"type": "env", "variable": "KIMI_CODE_API_KEY", "mapping": {"value": "api_key"}},
                {
                    "type": "env",
                    "variable": "KIMI_AUTH_TOKEN",
                    "mapping": {"value": "session_cookie"},
                },
                {
                    # Kimi Code CLI OAuth credential — the access token is read-only
                    # (never the refresh token); the server checks expires_at freshness.
                    # kimi-cli writes a per-install env file (kimi-code-env-<hash>.json),
                    # so the rule globs the credentials dir; freshest match wins.
                    "type": "file",
                    "paths": ["~/.kimi-code/credentials/kimi-code*.json"],
                    "format": "json",
                    "mapping": {
                        "access_token": "cli_access_token",
                        "expires_at": "cli_expires_at",
                    },
                },
                {
                    "type": "cookie",
                    "domains": ["kimi.moonshot.cn", "kimi.com"],
                    "name": "kimi-auth",
                    "mapping": {"value": "session_cookie"},
                },
            ],
        },
        "zai": {
            "name": "zAI API",
            "icon": "\ud83c\udf10",
            "rules": [{"type": "env", "variable": "ZAI_API_KEY", "mapping": {"value": "api_key"}}],
        },
        "opencode": {
            "name": "OpenCode",
            "icon": "\u26a1",
            "rules": [
                {
                    "type": "cookie",
                    "domains": ["opencode.ai", ".opencode.ai"],
                    "name": "auth",
                    "mapping": {"value": "cookie_session"},
                },
            ],
        },
        "antigravity": {
            "name": "Antigravity",
            "icon": "\ud83d\udef8",
            "rules": [
                {
                    # Ship the OAuth token so multi-host servers can call the
                    # Code Assist cloud API without accessing the local file.
                    "type": "file",
                    "paths": [
                        "~/.gemini/antigravity-cli/antigravity-oauth-token",
                    ],
                    "format": "json",
                    "mapping": {
                        "token.access_token": "oauth_token",
                        "token.refresh_token": "refresh_token",
                        # Raw ISO8601 expiry — converted to expiry_date (ms epoch)
                        # below so the server's token_cache can compare freshness.
                        "token.expiry": "_raw_expiry",
                    },
                },
            ],
        },
        "ollama": {
            "name": "Ollama Cloud",
            "icon": "\ud83e\udd99",
            "rules": [
                {
                    "type": "env",
                    "variable": "OLLAMA_SESSION_TOKEN",
                    "mapping": {"value": "cookie_session"},
                },
                {
                    "type": "cookie",
                    "domains": ["ollama.com", ".ollama.com"],
                    "name": "session",
                    "mapping": {"value": "cookie_session"},
                },
                {
                    "type": "cookie",
                    "domains": ["ollama.com", ".ollama.com"],
                    "name": "ollama_session",
                    "mapping": {"value": "cookie_session"},
                },
                {
                    "type": "cookie",
                    "domains": ["ollama.com", ".ollama.com"],
                    "name": "__Host-ollama_session",
                    "mapping": {"value": "cookie_session"},
                },
                {
                    "type": "cookie",
                    "domains": ["ollama.com", ".ollama.com"],
                    "name": "__Secure-next-auth.session-token",
                    "mapping": {"value": "cookie_session"},
                },
                {
                    "type": "cookie",
                    "domains": ["ollama.com", ".ollama.com"],
                    "name": "__Secure-session",
                    "mapping": {"value": "cookie_session"},
                },
                {
                    "type": "cookie",
                    "domains": ["ollama.com", ".ollama.com", "signin.ollama.com"],
                    "name": "access-token",
                    "mapping": {"value": "cookie_session"},
                },
            ],
        },
    }
}
# -------------------------

# --- Configuration ---

DEFAULT_CONFIG = {
    "heartbeat_seconds": 60,
    "providers": ["all"],
    "retry_attempts": 3,
    "retry_backoff_seconds": 5,
    "queue_max_size_mb": 10,
    "log_level": "INFO",
    "log_file_enabled": True,
    # NOTE: `auto_update` is intentionally NOT defaulted here. It is tri-state:
    # explicitly true/false in the user's config is a local override, while
    # absent (the default) defers to the server's fleet-wide `sidecar_auto_update`
    # flag. See _auto_update_enabled() and scripts/sidecar_pkg/self_update.py.
}

REQUIRED_CONFIG_FIELDS = ["api_url", "api_key"]

# Global state for daemon mode
_daemon_running = False
_pid_file_path: Path | None = None
_hostname: str | None = None
_windows_cred_cache: dict = {}
_windows_cred_ttl_seconds: int = 300
# Update channel the server tells us to track ("stable" | "edge"), refreshed
# from each /fleet/ingest response. None until the first successful check-in;
# the update-check thread then falls back to the channel inferred from our own
# version string.
_UPDATE_CHANNEL: str | None = None
# Auto-update preference, resolved with "local override wins" precedence:
#   _AUTO_UPDATE_LOCAL  — explicit local config (`auto_update`); None = defer to server.
#   _AUTO_UPDATE_SERVER — fleet-wide flag from the /fleet/ingest response.
# Effective value comes from _auto_update_enabled().
_AUTO_UPDATE_LOCAL: bool | None = None
_AUTO_UPDATE_SERVER: bool = False


def _auto_update_enabled() -> bool:
    """Effective auto-update setting: explicit local config overrides the server."""
    return _AUTO_UPDATE_LOCAL if _AUTO_UPDATE_LOCAL is not None else _AUTO_UPDATE_SERVER


def get_sidecar_dir() -> Path:
    """Get the sidecar configuration directory.

    Honours RUNWAY_CONFIG_DIR for parity with the server (set via .env in
    dev) so `make dev` and `make sidecar` share the same project-local
    config when invoked from the repo. Falls back to the platform default
    otherwise.
    """
    override = os.getenv("RUNWAY_CONFIG_DIR")
    if override:
        return Path(override) / "sidecar"
    if platform.system() == "Windows":
        app_data = os.getenv("APPDATA")
        if app_data:
            return Path(app_data) / "runway" / "sidecar"
        return Path.home() / "AppData" / "Roaming" / "runway" / "sidecar"
    return Path.home() / ".config" / "runway" / "sidecar"


def get_queue_dir() -> Path:
    """Get the queue directory for offline storage."""
    return get_sidecar_dir() / "queue"


def get_log_path() -> Path:
    """Get the log file path."""
    return get_sidecar_dir() / "sidecar.log"


def _tail_log(n: int = 20) -> list[str]:
    """Return the last *n* lines of the sidecar log file (best-effort)."""
    try:
        path = get_log_path()
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [line.rstrip() for line in lines[-n:]]
    except Exception:
        return []


def get_pid_file_path() -> Path:
    """Get the PID file path."""
    return get_sidecar_dir() / "sidecar.pid"


def get_hostname() -> str:
    """Get the cached, normalized sidecar id (call gethostname() once).

    Normalized to a stable id (lowercased first DNS label) so a host that flips
    between its FQDN and `.local`/short name doesn't register as duplicate
    sidecars. See scripts/sidecar_pkg/identity.py.
    """
    global _hostname
    if _hostname is None:
        from scripts.sidecar_pkg.identity import normalize_sidecar_id

        _hostname = normalize_sidecar_id(socket.gethostname())
    return _hostname


def ensure_dirs() -> None:
    """Ensure all required directories exist."""
    get_sidecar_dir().mkdir(parents=True, exist_ok=True)
    get_queue_dir().mkdir(parents=True, exist_ok=True)


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load configuration from file or create template if missing."""
    if config_path:
        config_file = Path(config_path)
    else:
        config_file = get_sidecar_dir() / "config.json"

    if not config_file.exists():
        ensure_dirs()
        template = {
            "api_url": "http://your-server:8765",
            "api_key": "your-secret-key",
            "heartbeat_seconds": 60,
            "providers": ["all"],
            "retry_attempts": 3,
            "retry_backoff_seconds": 5,
            "queue_max_size_mb": 10,
            "log_level": "INFO",
            "log_file_enabled": True,
        }
        # Write with owner-only permissions: the file holds the ingest API
        # key, so a world-readable default umask (0644) would let any local
        # user on the box read the secret.
        body = json.dumps(template, indent=2).encode("utf-8")
        flags = os.O_CREAT | os.O_WRONLY | os.O_EXCL
        try:
            fd = os.open(str(config_file), flags, 0o600)
        except FileExistsError:
            # Created by another process between exists() and open(); fall
            # through to the normal "config exists" path on next call.
            pass
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
        try:
            os.chmod(config_file, 0o600)
        except OSError:
            pass  # best-effort on platforms without POSIX perms
        print(f"ERROR: Config file created at {config_file}")
        print("Please edit and add your api_url and api_key")
        sys.exit(1)

    try:
        with open(config_file) as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in config file: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Cannot read config file: {e}")
        sys.exit(1)

    # Environment variable overrides (useful for dev / docker / CI without
    # editing the config file). INGEST_API_KEY is the server-side name for
    # the same secret, so sourcing the project .env file works for both.
    if os.environ.get("RUNWAY_API_URL"):
        config["api_url"] = os.environ["RUNWAY_API_URL"]
    api_key_env = os.environ.get("RUNWAY_API_KEY") or os.environ.get("INGEST_API_KEY")
    if api_key_env:
        config["api_key"] = api_key_env

    # Validate required fields
    missing = [f for f in REQUIRED_CONFIG_FIELDS if f not in config or not config[f]]
    if missing:
        print(f"ERROR: Missing required config fields: {', '.join(missing)}")
        print(f"Config file: {config_file}")
        print("Tip: you can also set RUNWAY_API_URL / RUNWAY_API_KEY env vars.")
        sys.exit(1)

    # Apply defaults for optional fields
    for key, value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = value

    return config


# --- Logging Setup ---


def setup_logging(log_level: str, file_enabled: bool) -> None:
    """Configure logging with console and optional file output."""
    from logging.handlers import RotatingFileHandler

    # Respect TZ env var so log timestamps render in the user's local zone
    # rather than the host default. `make sidecar` exports .env into the
    # process env, so TZ propagates from there; in Docker the container env
    # provides it; on a bare invocation, the OS-level TZ wins.
    if hasattr(time, "tzset"):
        time.tzset()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if file_enabled:
        ensure_dirs()
        log_path = get_log_path()
        file_handler = RotatingFileHandler(
            log_path, mode="a", maxBytes=5 * 1024 * 1024, backupCount=3
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        handlers.append(file_handler)

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )


# --- PID File Management ---


def _pid_is_alive(pid: int) -> bool:
    """Return True if a process with `pid` is currently running."""
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(1, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it; treat as alive so we don't
        # clobber another user's sidecar.
        return True
    except OSError:
        return False
    return True


def write_pid_file() -> bool:
    """Write PID file atomically. Returns False if another sidecar holds it.

    Uses O_CREAT|O_EXCL to remove the TOCTOU between "check exists" and
    "write" — two daemons racing here used to be able to both claim the
    file because each saw it absent before either wrote.
    """
    global _pid_file_path
    _pid_file_path = get_pid_file_path()

    pid_bytes = str(os.getpid()).encode("ascii")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    while True:
        try:
            fd = os.open(str(_pid_file_path), flags, 0o600)
        except FileExistsError:
            # Existing PID file — read it and decide whether it's stale.
            try:
                old_pid = int(_pid_file_path.read_text().strip())
            except (OSError, ValueError):
                # Corrupt file: remove and retry.
                try:
                    _pid_file_path.unlink()
                except OSError:
                    return False
                continue
            if _pid_is_alive(old_pid):
                logging.error(f"Sidecar already running (PID: {old_pid})")
                return False
            # Stale PID — unlink and retry exclusive create.
            try:
                _pid_file_path.unlink()
            except OSError:
                return False
            continue
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(pid_bytes)
            break

    # Cache hostname after initialization
    get_hostname()
    return True


def remove_pid_file() -> None:
    """Remove PID file on exit."""
    global _pid_file_path
    if _pid_file_path and _pid_file_path.exists():
        try:
            _pid_file_path.unlink()
        except Exception:
            # Best-effort cleanup; ignore if the PID file is already gone or unwritable.
            pass


def cleanup() -> None:
    """Cleanup on exit."""
    global _daemon_running
    _daemon_running = False
    remove_pid_file()
    # Clear credential cache on exit
    global _windows_cred_cache
    _windows_cred_cache = {}
    logging.info("Sidecar shutdown complete")


# --- Signal Handlers ---


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global _daemon_running
    sig_name = signal.Signals(signum).name
    logging.info(f"Received {sig_name}, shutting down...")
    _daemon_running = False
    sys.exit(0)


def setup_signal_handlers() -> None:
    """Setup signal handlers for graceful shutdown.

    SIGHUP is ignored rather than treated as shutdown: a background
    sidecar whose launching terminal closes gets SIGHUP, and we don't
    want that to kill it. The config file watcher in sidecar_app/config.py
    handles live config reloads — there's no separate reload signal here.
    """
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)


# --- Queue Management ---


def queue_push(payload: dict[str, Any]) -> None:
    """Add payload to offline queue."""
    ensure_dirs()
    queue_dir = get_queue_dir()

    # Create queue file for today
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    queue_file = queue_dir / f"{today}.jsonl"

    entry = {"ts": int(time.time()), "payload": payload}

    with open(queue_file, "a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    logging.info(f"Queued payload for retry: {queue_file.name}")
    queue_rotate()


def queue_rotate(max_size_mb: int = 10, config: dict[str, Any] | None = None) -> None:
    """Rotate queue files, removing oldest if total size exceeds limit."""
    queue_dir = get_queue_dir()
    if not queue_dir.exists():
        return

    if max_size_mb is None and config:
        max_size_mb = config.get("queue_max_size_mb", 10)

    max_size_bytes = max_size_mb * 1024 * 1024

    # Get all queue files sorted by modification time (oldest first)
    queue_files = sorted(queue_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)

    # Calculate total size
    total_size = sum(f.stat().st_size for f in queue_files)

    # Remove oldest files until under limit
    while total_size > max_size_bytes and queue_files:
        oldest = queue_files.pop(0)
        try:
            size = oldest.stat().st_size
            oldest.unlink()
            total_size -= size
            logging.warning(f"Queue rotation: removed {oldest.name} ({size} bytes)")
        except Exception as e:
            logging.error(f"Failed to remove old queue file {oldest}: {e}")
            break


def queue_flush(
    api_url: str,
    api_key: str,
    stop_event: threading.Event | None = None,
) -> int:
    """Flush all queued payloads to server. Returns count of successful sends."""
    queue_dir = get_queue_dir()
    if not queue_dir.exists():
        return 0

    queue_files = sorted(queue_dir.glob("*.jsonl"))
    if not queue_files:
        return 0

    count = 0
    target_url = f"{api_url.rstrip('/')}/api/v1/fleet/ingest"

    for queue_file in queue_files:
        if stop_event and stop_event.is_set():
            logging.info("queue_flush: stop requested, aborting flush")
            break
        try:
            with open(queue_file) as f:
                lines = f.readlines()

            failed_lines = []
            for line in lines:
                if stop_event and stop_event.is_set():
                    logging.info("queue_flush: stop requested, aborting flush")
                    return count
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                    payload = entry.get("payload", {})

                    success, _, _ = http_post_signed_with_retry(
                        target_url, payload, api_key, stop_event=stop_event
                    )

                    if success:
                        count += 1
                    else:
                        failed_lines.append(line)
                except json.JSONDecodeError:
                    logging.error(f"Invalid JSON in queue file: {line[:100]}")
                except Exception as e:
                    logging.error(f"Failed to send queued payload: {e}")
                    failed_lines.append(line)

            # Remove file if all sent successfully, otherwise rewrite with failures
            if not failed_lines:
                queue_file.unlink()
                logging.info(f"Queue file processed and removed: {queue_file.name}")
            else:
                with open(queue_file, "w") as f:
                    for line in failed_lines:
                        f.write(line + "\n")
                logging.warning(
                    f"Queue file has {len(failed_lines)} failed entries: {queue_file.name}"
                )

        except Exception as e:
            logging.error(f"Failed to process queue file {queue_file}: {e}")

    return count


# --- HTTP Utilities ---


def build_ssl_context(api_url: str, config: dict[str, Any] | None = None) -> ssl.SSLContext | None:
    """Resolve a TLS trust store for HTTPS pushes to the Runway server.

    Returns ``None`` for plaintext ``http://`` URLs (urllib needs no context).
    Honours the ``ca_bundle`` / ``tls_insecure`` config keys (and their
    ``RUNWAY_CA_BUNDLE`` / ``RUNWAY_INSECURE`` env equivalents); see
    ``scripts.sidecar_pkg.tls.build_context`` for the full resolution order. The
    frozen sidecar bundles ``certifi`` so a valid public cert verifies without a
    system CA store.
    """
    from scripts.sidecar_pkg.tls import build_context

    config = config or {}
    cfg_insecure = str(config.get("tls_insecure", "")).strip().lower() in ("1", "true", "yes", "on")
    return build_context(
        api_url,
        ca_bundle=config.get("ca_bundle"),
        insecure=True if cfg_insecure else None,
    )


def health_check(api_url: str, timeout: int = 5) -> bool:
    """Check if server is healthy before pushing."""
    try:
        req = request.Request(f"{api_url.rstrip('/')}/api/health", method="GET")
        with request.urlopen(req, timeout=timeout, context=build_ssl_context(api_url)) as resp:
            return resp.getcode() == 200
    except Exception:
        return False


def http_post_signed(url: str, data: dict[str, Any], api_key: str) -> tuple[bool, Any, int]:
    """POST data to URL with HMAC-SHA256 signature. Returns (success, data, code)."""
    timestamp = str(int(time.time()))
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")

    signature = hmac.new(api_key.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Timestamp": timestamp,
    }

    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=15, context=build_ssl_context(url)) as resp:
            return True, json.loads(resp.read().decode("utf-8")), resp.getcode()
    except error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode("utf-8")), e.code
        except Exception:
            return False, e.reason, e.code
    except Exception as e:
        return False, str(e), 0


def http_post_signed_with_retry(
    url: str,
    data: dict[str, Any],
    api_key: str,
    max_attempts: int = 3,
    backoff_seconds: int = 5,
    stop_event: threading.Event | None = None,
) -> tuple[bool, Any, int]:
    """POST with exponential backoff retry.

    If *stop_event* is provided the inter-attempt sleep is interruptible: the
    function returns early with a failure result as soon as the event is set.
    """
    last_error = None
    last_code = 500

    for attempt in range(max_attempts):
        success, result, code = http_post_signed(url, data, api_key)

        if success:
            return True, result, code

        last_error = result
        last_code = code

        # Don't retry on client errors (4xx) except 429 (rate limit)
        if 400 <= code < 500 and code != 429:
            logging.error(f"HTTP {code}: {result} (no retry)")
            return False, result, code

        if attempt < max_attempts - 1:
            wait = backoff_seconds * (2**attempt)
            logging.warning(f"Attempt {attempt + 1} failed, retrying in {wait}s...")
            if stop_event is not None:
                if stop_event.wait(timeout=wait):
                    # Stop was requested during the backoff sleep
                    return False, last_error, last_code
            else:
                time.sleep(wait)

    return False, last_error, last_code


def human_delta(target_dt):
    """Format datetime as human-readable delta."""
    if not target_dt:
        return "—"
    now = datetime.datetime.now(datetime.UTC)
    if isinstance(target_dt, (int, float)):
        target_dt = datetime.datetime.fromtimestamp(target_dt, tz=datetime.UTC)
    if target_dt.tzinfo is None:
        target_dt = target_dt.replace(tzinfo=datetime.UTC)
    diff = target_dt - now
    seconds = int(diff.total_seconds())
    if seconds < 0:
        return "Just now"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


# --- Platform Utilities ---


def get_platform_data_dir(app_name: str) -> Path:
    """Get the platform-specific directory for user data."""
    system = platform.system()
    home = Path.home()

    if system == "Windows":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / app_name
        return home / "AppData/Local" / app_name
    if system == "Darwin":
        return home / "Library/Application Support" / app_name
    xdg_data_home = os.getenv("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / app_name
    return home / ".local/share" / app_name


def get_platform_config_dir(app_name: str) -> Path:
    """Get the platform-specific directory for user configuration."""
    if app_name == "runway":
        override = os.getenv("RUNWAY_CONFIG_DIR")
        if override:
            return Path(override)

    system = platform.system()
    home = Path.home()

    if system == "Windows":
        app_data = os.getenv("APPDATA")
        if app_data:
            return Path(app_data) / app_name
        return home / "AppData/Roaming" / app_name
    if system == "Darwin":
        return home / "Library/Application Support" / app_name
    xdg_config_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / app_name
    return home / ".config" / app_name


def resolve_path(path_str: str) -> Path:
    """Resolve registry placeholders in path strings."""
    if path_str.startswith("~"):
        path_str = os.path.expanduser(path_str)

    if "{{CONFIG_DIR:" in path_str:
        match = re.search(r"{{CONFIG_DIR:([^}]+)}}", path_str)
        if match:
            app_name = match.group(1)
            path_str = path_str.replace(match.group(0), str(get_platform_config_dir(app_name)))

    if "{{DATA_DIR:" in path_str:
        match = re.search(r"{{DATA_DIR:([^}]+)}}", path_str)
        if match:
            app_name = match.group(1)
            path_str = path_str.replace(match.group(0), str(get_platform_data_dir(app_name)))

    return Path(path_str)


def has_unexpired_token(path: Path) -> bool:
    """Best-effort freshness probe: True when the file's JSON carries an
    '*expires*' key with a numeric epoch value still in the future (+60s
    skew). Unknown/absent/unparseable -> False (ordering falls back to mtime).
    """
    try:
        data = json.loads(path.read_text())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    now = time.time()
    for key, val in data.items():
        if "expires" in str(key).lower():
            try:
                if float(val) > now + 60:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def expand_file_rule_paths(paths: list) -> list:
    """Expand a file rule's path list to concrete existing files.

    Plain entries resolve exactly (existing behavior). Entries containing glob
    metacharacters (* ? [) are expanded against the filesystem — this applies
    to ANY file rule, so a future rule with a literal metachar in a filename
    would need escaping. Glob matches are sorted by (has_unexpired_token,
    mtime) ascending — expired files first, valid ones last, mtime ascending
    within each group. The scrape loop applies each file in order and later
    matches overwrite earlier ones, so the last file is the freshest *valid*
    credential (or the freshest expired one when nothing valid remains).
    """
    import glob as glob_module

    out = []
    for path_str in paths:
        resolved = resolve_path(path_str)
        if any(c in str(resolved) for c in "*?["):
            try:
                matches = sorted(
                    (Path(p) for p in glob_module.glob(str(resolved)) if Path(p).is_file()),
                    key=lambda p: (has_unexpired_token(p), p.stat().st_mtime),
                )
                out.extend(matches)
            except OSError:
                logging.debug("File rule glob failed for %s", resolved, exc_info=True)
        elif resolved.exists():
            out.append(resolved)
    return out


# --- Browser Cookie Extraction ---


def decrypt_chromium_cookie(encrypted_value, browser_name="Chrome"):
    """Decrypt a Chromium-based cookie value based on the current platform."""
    if not encrypted_value:
        return None
    system = platform.system()

    # macOS decryption
    if system == "Darwin":
        try:
            service = f"{browser_name} Safe Storage"
            if "Edge" in browser_name:
                service = "Microsoft Edge Safe Storage"

            cmd = ["security", "find-generic-password", "-s", service, "-w"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                return None

            password = result.stdout.strip()
            if encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11"):
                import hashlib

                from cryptography.hazmat.primitives.ciphers import (
                    Cipher,
                    algorithms,
                    modes,
                )

                salt = b"saltysalt"
                key = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), salt, 1003, 16)
                iv = b" " * 16
                raw_ciphertext = encrypted_value[3:]
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                decryptor = cipher.decryptor()
                decrypted = decryptor.update(raw_ciphertext) + decryptor.finalize()
                pad_len = decrypted[-1]
                if 1 <= pad_len <= 16:
                    return decrypted[:-pad_len].decode("utf-8")
        except Exception:
            logging.debug("macOS cookie decryption failed", exc_info=True)
        return None

    # Windows decryption
    if system == "Windows":
        try:

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [
                    ("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.wintypes.BYTE)),
                ]

            crypt32 = ctypes.windll.crypt32
            blob_in = DATA_BLOB()
            blob_in.cbData = len(encrypted_value)
            blob_in.pbData = ctypes.cast(encrypted_value, ctypes.POINTER(ctypes.wintypes.BYTE))
            blob_out = DATA_BLOB()
            if crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            ):
                buffer = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return buffer.decode("utf-8")
        except Exception:
            logging.debug("Windows cookie decryption failed", exc_info=True)
        return None

    # Linux decryption
    try:
        try:
            return encrypted_value.decode("utf-8")
        except Exception:
            logging.debug("Direct UTF-8 decode of Linux cookie failed, trying secretstorage")
        import hashlib

        # Try secretstorage for Chrome/Edge keys
        import secretstorage
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        password = None
        for item in collection.get_all_items():
            if item.get_label() in [
                "Chrome Safe Storage",
                "Chromium Safe Storage",
                "Microsoft Edge Safe Storage",
            ]:
                password = item.get_secret()
                break
        if password and (encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11")):
            salt = b"saltysalt"
            key = hashlib.pbkdf2_hmac("sha1", password, salt, 1003, 16)
            iv = b" " * 16
            raw_ciphertext = encrypted_value[3:]
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(raw_ciphertext) + decryptor.finalize()
            pad_len = decrypted[-1]
            if 1 <= pad_len <= 16:
                return decrypted[:-pad_len].decode("utf-8")
    except Exception:
        logging.debug("Linux cookie decryption failed", exc_info=True)
    return None


class BrowserCookieExtractor:
    """Unified extractor for cookies across multiple browsers."""

    @staticmethod
    def get_all_paths():
        system = platform.system()
        home = Path.home()
        results = []

        # 1. Chromium-based
        variants = [
            {
                "name": "Chrome",
                "darwin": "Google/Chrome",
                "linux": [".config/google-chrome"],
                "win": "Google/Chrome/User Data",
            },
            {
                "name": "Chromium",
                "darwin": "Chromium",
                "linux": [".config/chromium"],
                "win": "Chromium/User Data",
            },
            {
                "name": "Edge",
                "darwin": "Microsoft Edge",
                "linux": [".config/microsoft-edge"],
                "win": "Microsoft/Edge/User Data",
            },
        ]
        for v in variants:
            dirs = []
            if system == "Darwin":
                dirs.append(home / "Library/Application Support" / v["darwin"])
            elif system == "Windows":
                la = os.getenv("LOCALAPPDATA")
                dirs.append(Path(la) / v["win"] if la else home / "AppData/Local" / v["win"])
            else:
                for lp in v["linux"]:
                    dirs.append(home / lp)

            for base in dirs:
                if not base.exists():
                    continue
                for profile in ["Default", "Profile 1", "Profile 2"]:
                    for rel in [profile + "/Network/Cookies", profile + "/Cookies"]:
                        p = base / rel
                        if p.exists():
                            results.append({"browser": v["name"], "type": "chromium", "path": p})

        # 2. Linux Flatpak / Snap
        if system == "Linux":
            flatpak_bases = [
                home / ".var/app/com.google.Chrome/config/google-chrome",
                home / ".var/app/org.chromium.Chromium/config/chromium",
                home / ".var/app/com.microsoft.Edge/config/microsoft-edge",
            ]
            for base in flatpak_bases:
                if not base.exists():
                    continue
                for profile in ["Default", "Profile 1", "Profile 2"]:
                    for rel in [profile + "/Network/Cookies", profile + "/Cookies"]:
                        p = base / rel
                        if p.exists():
                            results.append(
                                {
                                    "browser": "Chromium (Flatpak)",
                                    "type": "chromium",
                                    "path": p,
                                }
                            )

            snap_bases = [
                home / "snap/chromium/common/chromium",
                home / "snap/firefox/common/.mozilla/firefox",
            ]
            for base in snap_bases:
                if not base.exists():
                    continue
                if "chromium" in str(base).lower():
                    for profile in ["Default", "Profile 1"]:
                        p = base / profile / "Cookies"
                        if p.exists():
                            results.append(
                                {
                                    "browser": "Chromium (Snap)",
                                    "type": "chromium",
                                    "path": p,
                                }
                            )
                elif "firefox" in str(base).lower():
                    for p in base.glob("*.default*"):
                        if (p / "cookies.sqlite").exists():
                            results.append(
                                {
                                    "browser": "Firefox (Snap)",
                                    "type": "firefox",
                                    "path": p / "cookies.sqlite",
                                }
                            )

        # 3. Firefox
        ff_dirs = []
        if system == "Darwin":
            ff_dirs.append(home / "Library/Application Support/Firefox/Profiles")
        elif system == "Windows":
            ff_dirs.append(home / "AppData/Roaming/Mozilla/Firefox/Profiles")
        else:
            ff_dirs.append(home / ".mozilla/firefox")
        for base in ff_dirs:
            if not base.exists():
                continue
            for p in base.glob("*.default*"):
                if (p / "cookies.sqlite").exists():
                    results.append(
                        {
                            "browser": "Firefox",
                            "type": "firefox",
                            "path": p / "cookies.sqlite",
                        }
                    )

        # 4. Safari
        if system == "Darwin" and (home / "Library/Cookies/Cookies.binarycookies").exists():
            results.append(
                {
                    "browser": "Safari",
                    "type": "safari",
                    "path": home / "Library/Cookies/Cookies.binarycookies",
                }
            )

        return results

    @staticmethod
    def parse_safari(path):
        """Minimal safari binary cookie parser."""
        try:
            with open(path, "rb") as f:
                if f.read(4) != b"cook":
                    return []
                num_pages = struct.unpack(">I", f.read(4))[0]
                page_sizes = [struct.unpack(">I", f.read(4))[0] for _ in range(num_pages)]
                all_cookies = []
                for size in page_sizes:
                    data = f.read(size)
                    if len(data) < 12:
                        continue
                    num_c = struct.unpack("<I", data[4:8])[0]
                    off = [
                        struct.unpack("<I", data[8 + (i * 4) : 12 + (i * 4)])[0]
                        for i in range(num_c)
                    ]
                    for o in off:
                        c = data[o:]
                        u_o, n_o = (
                            struct.unpack("<I", c[16:20])[0],
                            struct.unpack("<I", c[20:24])[0],
                        )
                        v_o = struct.unpack("<I", c[28:32])[0]

                        def r_s(at):
                            e = c.find(b"\x00", at)
                            return c[at:e].decode("utf-8", errors="replace") if e != -1 else ""

                        all_cookies.append(
                            {"domain": r_s(u_o), "name": r_s(n_o), "value": r_s(v_o)}
                        )
                return all_cookies
        except Exception:
            logging.debug("Failed to parse Safari cookies", exc_info=True)
            return []

    @staticmethod
    def get_cookie(domain, name):
        # Normalise the domain to its bare form (no leading dot) so we can
        # express a precise suffix match. The previous LIKE %{domain}% was
        # a substring match — "anthropic.com" matched "evil-anthropic.com"
        # and "anthropic.com.evil.org" alike, which is wrong both for
        # security and for correctness if a malicious cookie row ever
        # landed in the user's browser store.
        bare = (domain or "").lstrip(".")
        dotted = "." + bare
        like_subdomain = "%." + bare

        for target in BrowserCookieExtractor.get_all_paths():
            try:
                if target["type"] == "safari":
                    for c in BrowserCookieExtractor.parse_safari(target["path"]):
                        host = (c.get("domain") or "").lower()
                        if c["name"] == name and (
                            host == bare or host == dotted or host.endswith(dotted)
                        ):
                            return c["value"]
                else:
                    with sqlite3.connect(
                        f"file:{str(target['path'])}?mode=ro&uri=1", uri=True
                    ) as conn:
                        cursor = conn.cursor()
                        if target["type"] == "chromium":
                            cursor.execute(
                                "SELECT encrypted_value FROM cookies "
                                "WHERE name = ? "
                                "AND (host_key = ? OR host_key = ? OR host_key LIKE ?)",
                                (name, bare, dotted, like_subdomain),
                            )
                            row = cursor.fetchone()
                            if row:
                                val = decrypt_chromium_cookie(row[0], target["browser"])
                                if val:
                                    return val
                        else:  # Firefox
                            cursor.execute(
                                "SELECT value FROM moz_cookies "
                                "WHERE name = ? "
                                "AND (host = ? OR host = ? OR host LIKE ?)",
                                (name, bare, dotted, like_subdomain),
                            )
                            row = cursor.fetchone()
                            if row:
                                return row[0]
            except Exception:
                logging.debug("Cookie extraction failed for browser target", exc_info=True)
                continue
        return None


def get_windows_credential(target: str) -> str | None:
    """Extract credential from Windows Credential Manager with caching."""
    if platform.system() != "Windows":
        return None

    now = time.time()
    for cached_target, (password, ttl) in _windows_cred_cache.items():
        if now < ttl and cached_target == target:
            return password

    try:
        cmd = [
            "powershell",
            "-Command",
            f"(New-Object System.Net.NetworkCredential('', (Get-StoredCredential -Target '{target}').Password)).Password",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            password = result.stdout.strip()
            if password:
                _windows_cred_cache[target] = (
                    password,
                    time.time() + _windows_cred_ttl_seconds,
                )
                return password
    except Exception:
        logging.debug("Windows Credential Manager lookup failed for %r", target, exc_info=True)
    return None


# --- Anthropic JSONL Enrichment ---


def discover_anthropic_email() -> str:
    """Attempt to discover account email from credentials file."""
    for creds_path in [
        os.path.expanduser("~/.claude/.credentials.json"),
        os.path.expanduser("~/.config/claude/.credentials.json"),
        os.path.expanduser("~/.claude.json"),
    ]:
        if os.path.exists(creds_path):
            try:
                with open(creds_path) as f:
                    data = json.load(f)
                oauth_acc = data.get("oauthAccount", {})
                email = oauth_acc.get("emailAddress", "") or oauth_acc.get("email", "")
                if email:
                    return email
            except Exception:
                logging.debug("Failed to read Anthropic email from %s", creds_path, exc_info=True)
    return ""


# --- Account Email Helpers (JWT id_token extraction) ---


def _decode_id_token_email(id_token: str) -> str | None:
    """Extract email from a JWT id_token without verifying the signature."""
    try:
        import base64 as _base64

        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(_base64.urlsafe_b64decode(payload_b64))
        email = payload.get("email")
        return email if isinstance(email, str) and "@" in email else None
    except Exception:
        return None


def _gemini_account_email() -> str:
    """Read email from ~/.gemini/oauth_creds.json id_token; returns 'default' if unavailable."""
    cred_path = os.path.expanduser("~/.gemini/oauth_creds.json")
    try:
        with open(cred_path) as f:
            creds = json.load(f)
        email = _decode_id_token_email(creds.get("id_token", ""))
        return email or "default"
    except Exception:
        return "default"


def _ag_account_email() -> str:
    """Read email from the agy OAuth token file; returns 'default' if unavailable.

    The agy token file does not carry an id_token or email claim directly, so
    we return 'default' and let the server derive a stable account_id from the
    hashed access_token.  If the user's email is known from a previous
    collection cycle (stored in _ACCOUNT_IDENTITIES), that is preferred.
    """
    cached = _ACCOUNT_IDENTITIES.get("antigravity")
    if cached:
        return cached
    return "default"


def _discover_antigravity_db_paths() -> list[Path]:
    """Return all conversation SQLite DBs under the agy CLI conversations dir."""
    base = os.path.expanduser("~/.gemini/antigravity-cli/conversations")
    bp = Path(base)
    if not bp.is_dir():
        return []
    return list(bp.glob("*.db"))


def _codex_account_email() -> str:
    """Read email from ~/.codex/auth.json id_token; returns 'default' if unavailable."""
    candidate_paths = [
        os.path.expanduser("~/.codex/auth.json"),
    ]
    for cred_path in candidate_paths:
        try:
            with open(cred_path) as f:
                creds = json.load(f)
            tokens = creds.get("tokens", {})
            if isinstance(tokens, dict):
                email = _decode_id_token_email(tokens.get("id_token", ""))
                if email:
                    return email
        except Exception:
            continue
    return "default"


# Global state for server-provided identity mapping and reset anchors
_ACCOUNT_IDENTITIES: dict[str, str] = {}
_GLOBAL_RESET_ANCHORS: dict[str, dict[str, str]] = {}

# Module-level credential cache (issue #272). Populated lazily by
# ``run_collection`` from ``GET /api/v1/fleet/config``; cleared on restart.
# Lives at module scope so ``run_collection`` (which is the only writer)
# can share it with the ``DaemonRunner``-level fetch in the heartbeat path.
_CREDENTIAL_CACHE: Any = None


def _get_credential_cache() -> Any:
    """Lazily build the singleton CredentialCache (defers the import to
    keep the metrics-only path free of new deps)."""
    global _CREDENTIAL_CACHE
    if _CREDENTIAL_CACHE is None:
        from scripts.sidecar_pkg.credentials import CredentialCache

        _CREDENTIAL_CACHE = CredentialCache(ttl_seconds=600)
    return _CREDENTIAL_CACHE


# Provider IDs that have event extractors. Per-account iteration loops over
# each of these and stamps events with the resolved ``account_id``.
_EVENT_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "chatgpt", "gemini", "opencode", "antigravity"}
)

# Legacy single-account identity discovery, used when the server has no
# per-account config for a provider (back-compat for older servers and the
# fallback path). Each entry returns the ``account_id`` to use.
_LEGACY_EVENT_ACCOUNT_DISCOVERY: dict[str, Any] = {
    "anthropic": lambda: globals()["discover_anthropic_email"]() or "default",
    "chatgpt": lambda: globals()["_codex_account_email"]() or "default",
    "gemini": lambda: globals()["_gemini_account_email"]() or "default",
    # OpenCode's legacy helper requires a DB path argument, so wrap it.
    "opencode": (
        lambda: globals()["_opencode_account_email"](globals()["_discover_opencode_db_path"]())
    ),
    "antigravity": lambda: globals()["_ag_account_email"]() or "default",
}


def _extract_events_for_provider(
    provider_id: str,
    account_ids: list[str],
    *,
    watermark: Any,
    bootstrap_days: int,
    out_events: list[dict[str, Any]],
    server_account_tag_hints: dict[str, dict[str, str]] | None = None,
) -> None:
    """Run the event extractor for ``provider_id`` once per ``account_ids``,
    stamping each event with the resolved identity. Errors on one account
    don't block the others (issue #272 acceptance: "Handle partial
    failure: if one account's credentials fail to fetch / decrypt, others
    continue.").

    ``server_account_tag_hints`` carries the operator's resolved /
    auto-hints fetched from ``/fleet/config``'s ``account_tag_hints``
    payload. The opencode extractor needs them so events that
    ``_OC_CANONICAL_MAP`` retags to a canonical provider (e.g.
    ``minimax-coding-plan`` → ``minimax``) can land on the operator's
    chosen account_id rather than the synthetic ``"default"`` card —
    closes the MiniMax card-split.
    """
    if not account_ids:
        return
    # Lazy import — these are only needed in the events branch.
    from scripts.sidecar_pkg.event_extractors.anthropic import parse_anthropic_events
    from scripts.sidecar_pkg.event_extractors.antigravity import parse_antigravity_events
    from scripts.sidecar_pkg.event_extractors.chatgpt import parse_chatgpt_events
    from scripts.sidecar_pkg.event_extractors.gemini import parse_gemini_events
    from scripts.sidecar_pkg.event_extractors.opencode import parse_opencode_events

    dispatch: dict[str, Any] = {
        "anthropic": _make_account_extractor(parse_anthropic_events, _discover_anthropic_log_paths),
        "chatgpt": _make_account_extractor(parse_chatgpt_events, _discover_codex_log_paths),
        "gemini": _make_account_extractor(parse_gemini_events, _discover_gemini_log_paths),
        "opencode": _make_account_extractor_opencode(parse_opencode_events),
        "antigravity": _make_account_extractor_antigravity(parse_antigravity_events),
    }

    extractor = dispatch.get(provider_id)
    if extractor is None:
        return
    canonical_hints = _build_canonical_hints_for_provider(provider_id, server_account_tag_hints)

    for account_id in account_ids:
        try:
            evts = extractor(
                account_id,
                watermark,
                bootstrap_days,
                canonical_hints=canonical_hints,
            )
        except Exception as e:
            logging.warning(f"  [{provider_id}/{account_id}] event extraction error: {e}")
            continue
        if evts:
            logging.info(f"  [{provider_id}/{account_id}] {len(evts)} new event(s)")
            out_events.extend(e.model_dump(mode="json") for e in evts)


def _build_canonical_hints_for_provider(
    provider_id: str,
    server_account_tag_hints: dict[str, dict[str, str]] | None,
) -> dict[str, dict[str, str]] | None:
    """Forward the canonical-provider hints so the opencode extractor
    can retarget events onto the operator's chosen account_id when
    their ``_OC_CANONICAL_MAP`` entry maps to a canonical provider
    (e.g. ``minimax``, ``kimi_coding``, ``ollama``).

    The server emits auto-hints under the canonical key
    (``provider:minimax``), but the events branch iterates under the
    iterating provider (``provider:opencode``). Without this forward,
    an event retagged to ``minimax`` would land at ``(minimax,
    "default")`` even when the operator has a configured
    ``s3ntin318@gmail.com`` row.

    PR #318 round-2 review (Hermes warning #1): this forwarding is
    what closes the card-split. The branch-level ``scoped_accounts``
    previously walked every canonical provider too — that spread the
    MiniMax identity to unrelated opencode sub-providers
    (``opencode-openai``, ``opencode-anthropic``). The fix keeps
    ``scoped_accounts`` at the local identity and lets THIS forward
    (per-event inside the extractor) do the retag.

    Returns ``None`` for non-opencode providers — they don't have a
    canonical retag concept, so the extractor gets nothing to look up.
    """
    if provider_id != "opencode":
        return None
    # Lazy import: only loaded on the events branch, not on the token-card branch.
    from scripts.sidecar_pkg.event_extractors.opencode import _OC_CANONICAL_MAP

    canonical_hints: dict[str, dict[str, str]] = {}
    for canonical_provider_id, _ in _OC_CANONICAL_MAP.values():
        canonical_hint_map = (server_account_tag_hints or {}).get(canonical_provider_id, {})
        if canonical_hint_map:
            canonical_hints[canonical_provider_id] = dict(canonical_hint_map)
    return canonical_hints or None


def _make_account_extractor(parser: Any, paths_finder: Any) -> Any:
    """Bind a parser to a path-discovery callable so we can pass ``account_id``."""

    def _extract(
        account_id: str,
        watermark: Any,
        bootstrap_days: int,
        *,
        canonical_hints: dict[str, dict[str, str]] | None = None,  # noqa: ARG001 — accepted for signature parity
    ) -> list:
        paths = paths_finder()
        if not paths:
            return []
        if isinstance(paths, list):
            paths_iter = paths  # antigravity returns a list
        else:
            paths_iter = [paths]
        all_evts = []
        for p in paths_iter:
            since = watermark.last_pushed(__extract_provider_id(parser), account_id) or (
                datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=bootstrap_days)
            )
            all_evts.extend(parser(p, account_id=account_id, since=since))
        # Deduplicate by event_id across paths (antigravity emits one event
        # per DB file; if a user has multiple we may double-count).
        seen: set[str] = set()
        deduped = []
        for ev in all_evts:
            eid = ev.event_id
            if eid in seen:
                continue
            seen.add(eid)
            deduped.append(ev)
        return deduped

    return _extract


def _make_account_extractor_opencode(parser: Any) -> Any:
    def _extract(
        account_id: str,
        watermark: Any,
        bootstrap_days: int,
        *,
        canonical_hints: dict[str, dict[str, str]] | None = None,
    ) -> list:
        db_path = _discover_opencode_db_path()
        if db_path is None:
            return []
        since = watermark.last_pushed("opencode", account_id) or (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=bootstrap_days)
        )
        return parser(
            db_path,
            account_id=account_id,
            since=since,
            canonical_hints=canonical_hints,
        )

    return _extract


def _make_account_extractor_antigravity(parser: Any) -> Any:
    def _extract(
        account_id: str,
        watermark: Any,
        bootstrap_days: int,
        *,
        canonical_hints: dict[str, dict[str, str]] | None = None,  # noqa: ARG001 — accepted for signature parity
    ) -> list:
        db_paths = _discover_antigravity_db_paths()
        if not db_paths:
            return []
        since = watermark.last_pushed("antigravity", account_id) or (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=bootstrap_days)
        )
        return parser(db_paths, account_id=account_id, since=since)

    return _extract


def __extract_provider_id(parser: Any) -> str:
    """Return the provider_id a parser corresponds to. Used by the per-account
    watermark lookup so events from multiple accounts don't stomp on each
    other in the watermark file."""
    name = getattr(parser, "__module__", "")
    if "anthropic" in name:
        return "anthropic"
    if "chatgpt" in name:
        return "chatgpt"
    if "gemini" in name:
        return "gemini"
    return "unknown"


def _opencode_account_email(db_path: Path | None) -> str:
    """Read email from the OpenCode SQLite `account` table; returns 'default' if unavailable.

    The OpenCode CLI stores a single account row keyed by email. Using the
    email as account_id keeps sidecar-pushed events aligned with the cards
    emitted by the server's web collector.
    """
    # 1. Try server-provided identity hint (propagated from server web scraper)
    ident = _ACCOUNT_IDENTITIES.get("opencode")
    if ident:
        return ident

    # 2. Try environment variable
    env_label = os.getenv("OPENCODE_ACCOUNT_LABEL")
    if env_label:
        return env_label

    # 3. Fallback to local DB
    if db_path is not None and db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.cursor()
                cur.execute("SELECT email FROM account LIMIT 1")
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0])
            finally:
                conn.close()
        except Exception:
            logging.debug("Failed to read account email from OpenCode DB", exc_info=True)

    return "default"


# --- Generic Collector Engine ---


def credential_origin_for_provider(provider_id: str) -> str:
    """Return the stable credential-origin descriptor the sidecar
    reports for a provider's blocked card.

    Phase 1 (PR #288 / #290): one origin per provider per cycle,
    ``f"provider:{provider_id}"``. The hint lookup in
    ``GenericCollector.collect_provider``'s block guard and the
    ``credential_origin`` field of the manifest ``blocked_origins``
    entry both go through this helper so the guard and the manifest
    can't drift apart.

    Phase 2 (#289): widens to a per-rule origin list. The two call
    sites stay the same; only this helper changes — that's the
    seam.
    """
    return f"provider:{provider_id}"


class GenericCollector:
    """Orchestrates data collection based on registry rules."""

    @staticmethod
    def get_nested(data: Any, key_path: str) -> Any:
        """Get nested value from dict using dot notation or list of keys.
        Supports fallback syntax using '|' (e.g. 'pathA|pathB').
        """
        if isinstance(key_path, str) and "|" in key_path:
            for path in key_path.split("|"):
                val = GenericCollector.get_nested(data, path)
                if val:
                    return val
            return None

        if isinstance(key_path, str):
            keys = key_path.split(".")
        else:
            keys = key_path

        current = data
        for k in keys:
            if isinstance(current, dict):
                current = current.get(k)
            else:
                return None
        return current

    @staticmethod
    def collect_provider(
        provider_id: str,
        config: dict[str, Any],
        *,
        account_label_hints: dict[str, dict[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Run all rules for a single provider and return ``(results, blocked_origins)``.

        ``results`` is the list of metric cards (token cards + quota cards)
        suitable for shipping to ``/fleet/ingest``. ``blocked_origins`` is a
        list of ``{"provider_id": str, "credential_origin": str}`` entries
        describing the credentials whose ``account_id`` could not be
        resolved via local discovery or the ``account_label_hints`` hint
        map. Those cards were extracted but not shipped - the sidecar
        reports them via ``POST /fleet/credentials/manifest`` so the
        operator can resolve them in the fleet UI (silent-listener model;
        see PR #288).

        ``account_label_hints`` is ``{provider_id: {credential_origin: account_id, ...}}``,
        forwarded by ``run_collection`` from the server's most recent
        ``/fleet/config`` response. When a rule's ``origin_descriptor``
        matches a hint, the hinted ``account_id`` is used as the fallback
        before the block triggers.
        """
        results: list[dict[str, Any]] = []
        blocked_origins: list[dict[str, str]] = []
        tokens: dict[str, Any] = {}

        name = config.get("name", provider_id)
        icon = config.get("icon", "❓")
        rules = config.get("rules", [])
        provider_hints = (account_label_hints or {}).get(provider_id, {})

        for rule in rules:
            rule_type = rule.get("type")
            mapping = rule.get("mapping", {})

            # 1. Environment Variables
            if rule_type == "env":
                val = os.getenv(rule.get("variable"))
                if val:
                    target = mapping.get("value")
                    if target:
                        tokens[target] = val

            # 2. Local Files (JSON/YAML)
            elif rule_type == "file":
                # expand_file_rule_paths resolves plain paths exactly and
                # expands glob patterns (e.g. kimi-cli's per-install
                # kimi-code-env-<hash>.json), freshest match last.
                for path in expand_file_rule_paths(rule.get("paths", [])):
                    try:
                        fmt = rule.get("format", "json")
                        with open(path) as f:
                            if fmt == "yaml":
                                # Very basic YAML parser for zero-dependency
                                # Only supports simple key: value
                                content = f.read()
                                data = {}
                                for line in content.splitlines():
                                    if ":" in line:
                                        k, v = line.split(":", 1)
                                        data[k.strip()] = v.strip().strip('"').strip("'")
                            else:
                                data = json.load(f)

                        for key_path, target in mapping.items():
                            val = GenericCollector.get_nested(data, key_path)
                            if val:
                                tokens[target] = val
                        if tokens:
                            logging.info(f"  [{provider_id}] token file matched: {path}")
                    except Exception as e:
                        logging.debug(f"Error reading file {path}: {e}")

            # 3. macOS Keychain
            elif rule_type == "keychain" and platform.system() == "Darwin":
                try:
                    cmd = [
                        "security",
                        "find-generic-password",
                        "-s",
                        rule.get("service_name"),
                        "-w",
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    if result.returncode == 0:
                        raw = result.stdout.strip()
                        fmt = rule.get("format", "raw")
                        if fmt == "json":
                            data = json.loads(raw)
                            for key_path, target in mapping.items():
                                val = GenericCollector.get_nested(data, key_path)
                                if val:
                                    tokens[target] = val
                        else:
                            target = mapping.get("value")
                            if target:
                                tokens[target] = raw
                except Exception:
                    logging.debug("File credential extraction failed", exc_info=True)

            # 4. Windows Credential Manager
            elif rule_type == "windows_credential" and platform.system() == "Windows":
                val = get_windows_credential(rule.get("target"))
                if val:
                    target = mapping.get("value")
                    if target:
                        tokens[target] = val

            # 5. Browser Cookies
            elif rule_type == "cookie":
                name_to_find = rule.get("name")
                for domain in rule.get("domains", []):
                    val = BrowserCookieExtractor.get_cookie(domain, name_to_find)
                    if val:
                        target = mapping.get("value")
                        if target:
                            tokens[target] = val
                            logging.info(
                                f"  [{provider_id}] cookie '{name_to_find}' found on {domain}"
                            )
                            break

            # 6. Execute Command (e.g. git config)
            elif rule_type == "exec":
                try:
                    cmd = rule.get("command")
                    if cmd:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            val = result.stdout.strip()
                            if val:
                                target = mapping.get("value")
                                if target:
                                    tokens[target] = val
                except Exception:
                    logging.debug("exec credential rule failed", exc_info=True)

            # 7. Specialized: SQLite (OpenCode)
            elif rule_type == "sqlite":
                for path_str in rule.get("paths", []):
                    path = resolve_path(path_str)
                    if path.exists():
                        try:
                            conn = sqlite3.connect(str(path))
                            try:
                                cursor = conn.cursor()
                                now = datetime.datetime.now(datetime.UTC)
                                # Discover identity
                                hostname = get_hostname()
                                acc_label = os.getenv("OPENCODE_ACCOUNT_LABEL")
                                if not acc_label:
                                    try:
                                        cursor.execute("SELECT email FROM account LIMIT 1")
                                        row = cursor.fetchone()
                                        if row and row[0]:
                                            acc_label = row[0]
                                    except Exception:
                                        logging.debug(
                                            "OpenCode account email query failed", exc_info=True
                                        )

                                for q in rule.get("queries", []):
                                    query_str = q.get("query")
                                    for window_name, seconds in q.get("windows", {}).items():
                                        cutoff = int(
                                            (now - datetime.timedelta(seconds=seconds)).timestamp()
                                            * 1000
                                        )
                                        cursor.execute(query_str, (cutoff,))
                                        row = cursor.fetchone()
                                        used = float(row[0] or 0.0)
                                        count = int(row[1] or 0)
                                        limit = q.get("limits", {}).get(window_name, 1.0)
                                        remaining = max(0, limit - used)
                                        pct = (used / limit * 100) if limit > 0 else 0

                                        results.append(
                                            {
                                                "service_name": f"{provider_id.capitalize()} ({window_name})",
                                                "icon": icon,
                                                "remaining": f"${remaining:.2f}"
                                                if "$" in q.get("name", "") or "cost" in query_str
                                                else f"{remaining}",
                                                "unit": f"{limit} limit",
                                                "reset": f"Rolling {window_name}",
                                                "health": "good"
                                                if pct < 70
                                                else "warning"
                                                if pct < 90
                                                else "critical",
                                                "pace": "Stable" if pct < 50 else "High",
                                                "detail": f"{used} used · {count} msgs · {hostname} [Sidecar]",
                                                "data_source": "local",
                                                "account_label": acc_label,
                                                "metadata": {
                                                    "used": used,
                                                    "count": count,
                                                    "window": window_name,
                                                    "hostname": hostname,
                                                    "account_label": acc_label,
                                                },
                                            }
                                        )
                            finally:
                                conn.close()
                        except Exception as e:
                            logging.debug(f"SQLite error for {provider_id}: {e}")

            # 8. Specialized: Claude Statusline
            elif rule_type == "file_json_statusline":
                for path_str in rule.get("paths", []):
                    path = resolve_path(path_str)
                    if path.exists():
                        try:
                            # Freshness check (5 minutes)
                            mtime = os.path.getmtime(path)
                            if (time.time() - mtime) > 300:
                                continue

                            with open(path) as f:
                                data = json.load(f)

                            email = discover_anthropic_email()
                            now_str = datetime.datetime.now(datetime.UTC).isoformat()
                            name_map = {"five_hour": "Session Window", "seven_day": "Weekly Window"}

                            # Rate Limits
                            limits = data.get("rate_limits", {})
                            for key, info in limits.items():
                                u_type = name_map.get(key, key.replace("_", " ").title())
                                pct_used = float(info.get("used_percentage", 0.0))
                                reset_ts = info.get("resets_at")
                                results.append(
                                    {
                                        "service_name": f"Claude ({u_type})",
                                        "icon": icon,
                                        "remaining": f"{(100 - pct_used):.1f}%",
                                        "unit": "capacity",
                                        "reset": str(datetime.datetime.fromtimestamp(reset_ts))
                                        if reset_ts
                                        else "—",
                                        "health": "good" if pct_used < 70 else "warning",
                                        "pace": "Active",
                                        "detail": f"{pct_used:.1f}% used [Sidecar]",
                                        "data_source": "local",
                                        "account_id": email or None,
                                        "account_label": email or None,
                                        "metadata": {"used": pct_used, "resets_at": reset_ts},
                                    }
                                )

                            # Context / Tokens
                            ctx = data.get("context_window", {})
                            if ctx:
                                total_tokens = ctx.get("total_input_tokens", 0) + ctx.get(
                                    "total_output_tokens", 0
                                )
                                max_t = ctx.get("max_tokens", 200000)
                                results.append(
                                    {
                                        "service_name": "Claude (Session Tokens)",
                                        "icon": "🪙",
                                        "remaining": f"{total_tokens:,}",
                                        "unit": f"/ {max_t:,}",
                                        "reset": data.get("model", {}).get(
                                            "display_name", "Sonnet"
                                        ),
                                        "health": "good",
                                        "pace": "Active",
                                        "detail": f"{total_tokens:,} tokens [Sidecar]",
                                        "data_source": "local",
                                        "account_id": email or None,
                                        "account_label": email or None,
                                    }
                                )
                        except Exception:
                            logging.debug("Statusline file rule failed", exc_info=True)

        # Convert antigravity's raw ISO8601 token.expiry into expiry_date (ms
        # epoch, matching gemini's oauth_creds.json convention) so the server's
        # token_cache._is_staler can compare freshness. Without this, agy's
        # opaque access token carries no exp signal at all, so an expired local
        # token from one sidecar silently clobbers a valid one pushed by
        # another (see docs/plans — the "app shows 55%, agy shows 0%" incident).
        # If the local agy session has actually lapsed, don't push the dead
        # credential at all: agy owns its own refresh cycle (no client_id in
        # the file for Runway to refresh with), so pushing it would just keep
        # re-poisoning the shared cache entry every cycle until the user re-runs
        # agy. Let a sidecar with a live agy session keep serving quota instead.
        if provider_id == "antigravity" and tokens.get("_raw_expiry"):
            raw_expiry = tokens.pop("_raw_expiry")
            try:
                expiry_dt = datetime.datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
                if expiry_dt.timestamp() < time.time():
                    logging.warning(
                        f"  [{provider_id}] local token expired at {raw_expiry} — "
                        "not pushing (run `agy` to refresh)"
                    )
                    tokens.pop("oauth_token", None)
                    tokens.pop("refresh_token", None)
                else:
                    tokens["expiry_date"] = str(int(expiry_dt.timestamp() * 1000))
            except (ValueError, TypeError):
                logging.debug(f"  [{provider_id}] could not parse token expiry: {raw_expiry!r}")

        # Stamp the antigravity token with the resolved account identity. The agy
        # token file carries no id_token/email, so without this the server hashes
        # the refresh_token into a cache key that won't match the email-seeded
        # collector identity (durable seeding from LatestUsage) — leaving quota
        # blank. _ag_account_email() returns the email once the server has
        # propagated it (else "default", caught by the server's "default" cache
        # fallback). Mirrors how gemini/opencode/codex key their cards on the email.
        if provider_id == "antigravity" and tokens and not tokens.get("account_id"):
            tokens["account_id"] = _ag_account_email()

        # Stamp the codex/chatgpt token with the resolved account identity.
        # The registry's file-rule mapping copies auth.json's own
        # `tokens.account_id` through verbatim — Codex CLI's internal OpenAI
        # account UUID — but the server's durable collector identity is
        # seeded from LatestUsage's historical account_id, which is the
        # email (see event extraction above, which already uses
        # _codex_account_email()). The UUID and the email never match as a
        # token_cache key, so the collector's lookup misses and silently
        # falls back to whatever's cached under "default" (typically a
        # stale dashboard-configured cookie). Unconditionally override: a
        # resolved email is always the better identity here, not just a
        # fallback for an absent value.
        if provider_id == "chatgpt" and tokens:
            tokens["account_id"] = _codex_account_email()

        # If tokens were extracted, add a hidden token card
        if tokens:
            logging.info(f"  [{provider_id}] tokens extracted: {list(tokens.keys())}")

            if any(k.startswith("cookie_") for k in tokens):
                unit = "cookie"
                data_source = "web"
            elif "api_key" in tokens:
                unit = "api_key"
                data_source = "api"
            else:
                unit = "oauth"
                data_source = "api"

            # Apply the silent-listener block guard (PR #288, fixed in
            # PR #290): if no ``account_id`` is resolvable for this
            # provider's tokens, the card must not ship. The card never
            # sees a server-side ``account_id`` of "default" or None —
            # operators see the unresolved origin in the fleet UI's
            # "Untagged credentials" panel and resolve it by selecting
            # a configured ``provider_configs`` row. Resolutions flow
            # back via ``/fleet/config``'s ``account_tag_hints`` on the
            # next cycle, OR via the ``resolved`` field of the manifest
            # response on the *same* cycle (PR #290 round-2 review).
            #
            # Phase-1 simplification: emit ONE credential origin per
            # provider per cycle (``provider:<provider_id>``). Per-rule
            # origin expansion is the follow-up issue.
            local_account_id = tokens.get("account_id")
            hint_account_id = provider_hints.get(credential_origin_for_provider(provider_id))
            if local_account_id is not None:
                resolved_account_id = local_account_id
            elif hint_account_id is not None:
                resolved_account_id = hint_account_id
                # Stamp the hint into ``tokens`` so the card's metadata
                # carries the operator-resolved ``account_id`` (the
                # server's ingest endpoint reads it from
                # ``metadata.account_id``).
                tokens["account_id"] = hint_account_id
                logging.info(
                    f"  [{provider_id}] token card stamped via server hint → "
                    f"account_id={hint_account_id}"
                )
            else:
                resolved_account_id = None

            if resolved_account_id is None:
                origin = credential_origin_for_provider(provider_id)
                logging.warning(
                    f"  [{provider_id}] token card blocked (origin={origin}) — "
                    "no account_id resolved (local discovery + server hint both empty); "
                    "not shipping. Operator will see this in the fleet view's "
                    "Untagged Credentials panel."
                )
                blocked_origins.append(
                    {
                        "provider_id": provider_id,
                        # Coarse per-provider descriptor — phase 2 will
                        # subdivide per-rule. Operators keying off this
                        # today get a single row per provider regardless
                        # of how many credentials the host has on disk.
                        "credential_origin": origin,
                    }
                )
            else:
                results.append(
                    {
                        "service_name": name,
                        "icon": icon,
                        "remaining": "Token",
                        "unit": unit,
                        "reset": "—",
                        "health": "good",
                        "pace": "Token",
                        "detail": "[Token Extracted] [Sidecar]",
                        "data_source": data_source,
                        "metadata": {**tokens, "provider_id": provider_id},
                    }
                )

        # Post-process: propagate discovered account identity to all cards.
        # When the token card was blocked above, ``tokens["account_id"]`` is
        # still useful for the quota cards this provider emits (none today —
        # quota cards don't come from the sidecar, see fleet.py:178), so the
        # propagation continues as before.
        acc_id = tokens.get("account_id")
        acc_label = tokens.get("account_label")
        if acc_id or acc_label:
            for card in results:
                if acc_id and not card.get("account_id"):
                    card["account_id"] = acc_id
                if acc_label and not card.get("account_label"):
                    card["account_label"] = acc_label

        return results, blocked_origins


# --- Main Loop ---


def _discover_anthropic_log_paths() -> list[Path]:
    """Return all .jsonl files under ~/.claude/projects (and CLAUDE_CONFIG_DIR)."""
    dirs: list[str] = []
    config_env = os.getenv("CLAUDE_CONFIG_DIR", "")
    if config_env:
        for p in config_env.split(","):
            p = p.strip()
            if not p:
                continue
            proj = os.path.join(p, "projects") if not p.endswith("/projects") else p
            if os.path.isdir(proj) and proj not in dirs:
                dirs.append(proj)
    for candidate in [
        os.path.expanduser("~/.claude/projects"),
        os.path.expanduser("~/.config/claude/projects"),
    ]:
        if os.path.isdir(candidate) and candidate not in dirs:
            dirs.append(candidate)
    paths: list[Path] = []
    for d in dirs:
        paths.extend(Path(d).glob("**/*.jsonl"))
    return paths


def _discover_codex_log_paths() -> list[Path]:
    """Return all .jsonl files under the Codex session directories."""
    candidate_dirs = [
        os.path.expanduser("~/.codex/sessions"),
        os.path.expanduser("~/.config/codex/sessions"),
    ]
    paths: list[Path] = []
    for d in candidate_dirs:
        dp = Path(d)
        if dp.is_dir():
            paths.extend(dp.glob("**/*.jsonl"))
    return paths


def _discover_gemini_log_paths() -> list[Path]:
    """Return all .jsonl session files under the Gemini session directories."""
    candidate_dirs = [
        os.path.expanduser("~/.gemini/tmp/ai-usage-tracker/chats"),
        os.path.expanduser("~/.gemini/tmp/gemini/chats"),
        os.path.expanduser("~/.gemini/tmp/sessions"),
        os.path.expanduser("~/.gemini/sessions"),
        os.path.expanduser("~/.config/gemini/sessions"),
    ]
    # Also scan worktree-specific chats dirs under ~/.gemini/tmp
    tmp_base = os.path.expanduser("~/.gemini/tmp")
    if os.path.isdir(tmp_base):
        for item in os.listdir(tmp_base):
            chats_dir = os.path.join(tmp_base, item, "chats")
            if os.path.isdir(chats_dir) and chats_dir not in candidate_dirs:
                candidate_dirs.append(chats_dir)
    paths: list[Path] = []
    for d in candidate_dirs:
        dp = Path(d)
        if dp.is_dir():
            paths.extend(dp.glob("session-*.jsonl"))
    return paths


def _discover_opencode_db_path() -> Path | None:
    """Return the path to the OpenCode SQLite database, or None if not found.

    The OpenCode CLI has shipped its SQLite database under two different
    locations: the XDG-conformant ``~/.local/share/opencode/opencode.db``
    (default) and the flatter ``~/.opencode/opencode.db`` (seen on some
    Linux installs). Check both so token usage events get ingested
    regardless of which the local install chose; the missing path is a
    no-op on platforms that use neither.
    """
    candidates = [
        os.path.expanduser("~/.local/share/opencode/opencode.db"),
        os.path.expanduser("~/.opencode/opencode.db"),
    ]
    for p in candidates:
        path = Path(p)
        if path.exists():
            return path
    return None


def _post_credential_manifest(
    *,
    api_url: str | None,
    api_key: str,
    sidecar_id: str,
    entries: list[dict[str, str]],
    on_resolved: Callable[[dict[str, dict[str, str]]], None] | None = None,
) -> None:
    """Send the silent-listener manifest POST (PR #288, PR #290 round-2 review).

    Gated on the same INGEST_API_KEY HMAC scheme as ``/fleet/ingest``.
    Returns ``None`` on any failure — the next cycle retries (the server
    retains the prior ``pending_credential_tags`` snapshot). The
    ``manifest`` endpoint itself returns 503 when INGEST_API_KEY is
    unset, which we just pass through silently.

    When the response is 200, the server's ``resolved`` field carries
    ``{provider_id: {credential_origin: account_id}}`` for any tag the
    operator set since the sidecar last refreshed its hint cache. The
    caller passes ``on_resolved`` to merge those hints into its cache
    immediately — that closes the silent-listener loop on the *same*
    cycle instead of waiting on the next ``/fleet/config`` round-trip
    (Hermes suggestion #9 in PR #290 review).
    """
    if not api_url or not api_key:
        return
    # Empty entries still ship: the server prunes any pending rows for our
    # sidecar_id that aren't re-reported, keeping the fleet UI in sync with
    # what the sidecar actually has on disk this cycle.
    body = json.dumps({"sidecar_id": sidecar_id, "entries": entries}).encode("utf-8")
    ts = str(int(time.time()))
    sig = hmac.new(
        api_key.encode("utf-8"),
        ts.encode() + body,
        hashlib.sha256,
    ).hexdigest()
    url = f"{api_url.rstrip('/')}/api/v1/fleet/credentials/manifest"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sig,
        },
    )
    from scripts.sidecar_pkg.tls import build_context  # late import keeps
    # stdlib-heavy sidecar slim when TLS is unused.

    try:
        with urllib.request.urlopen(req, timeout=5, context=build_context(url)) as resp:
            if resp.getcode() != 200:
                logging.debug(f"manifest: server returned {resp.getcode()}")
                return
            try:
                payload = json.loads(resp.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logging.debug(f"manifest: response not JSON ({exc})")
                return
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        logging.debug(f"manifest: skipped ({exc})")
        return

    if on_resolved is None:
        return
    resolved = payload.get("resolved") if isinstance(payload, dict) else None
    if not isinstance(resolved, dict) or not resolved:
        return
    resolved_count = sum(len(v) for v in resolved.values() if isinstance(v, dict))
    if resolved_count:
        logging.info(
            f"  manifest: server resolved {resolved_count} origin(s) on this cycle "
            f"(consuming into local hint cache)"
        )
    try:
        on_resolved(resolved)
    except Exception as exc:  # pragma: no cover — defensive only
        logging.debug(f"manifest: on_resolved callback raised ({exc})")


def run_collection(
    config: dict[str, Any],
    providers: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Run collection for specified or enabled providers.

    Returns (metrics, events, error_count) where events is a list of
    serialised UsageEventPush dicts ready for the wire payload.
    """
    # Lazy import — avoids requiring app/ in environments that only use metrics path.
    try:
        from scripts.sidecar_pkg.event_watermark import EventWatermark

        _watermark = EventWatermark(
            Path(os.path.expanduser("~/.config/runway-sidecar/event-watermark.json"))
        )
        _events_enabled = True
    except Exception as _e:
        logging.warning(f"Event extraction unavailable: {_e}")
        _watermark = None
        _events_enabled = False

    # Fetch the server-known per-account identity list (issue #272) and
    # the operator-resolved tag-hint map (PR #288 silent-listener). Both
    # views come from a single ``GET /api/v1/fleet/config`` round-trip
    # (PR #290 round-2 review — earlier code issued a second GET for
    # tag hints, doubling the request load on every heartbeat).
    #
    # The identity map is decoupled from credential_token issuance —
    # rows without credentials and configurations with an empty
    # INGEST_API_KEY still contribute their ``account_id`` here, so the
    # per-account attribution fix isn't gated on those conditions
    # (PR #283 review).
    #
    # On a fetch failure the cache retains its prior snapshot — the
    # next cycle retries; a transient outage shouldn't suppress hints
    # for the whole 10-min TTL (PR #283 round-3 review).
    #
    # The whole block (lazy import + cache init + network fetch) is
    # wrapped in a single guarded ``try`` so a failure at any point —
    # including missing credentials module on a frozen binary — falls
    # back to the legacy single-account path without killing
    # ``run_collection``.
    server_accounts_by_provider: dict[str, list[str]] = {}
    server_account_tag_hints: dict[str, dict[str, str]] = {}
    try:
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        cache = _get_credential_cache()
        api_url_for_tokens = os.environ.get("RUNWAY_API_URL") or config.get("api_url")
        if api_url_for_tokens and not cache.is_fresh():
            # ``sidecar_id`` (#319) lets the server scope
            # account_tag_hints to this machine's credential tags.
            fetched = fetch_identity_hints(api_url_for_tokens, sidecar_id=get_hostname())
            # ``fetched is None`` distinguishes outage from a valid
            # empty response. The cache skips the update on None, so
            # the prior snapshot survives and ``is_fresh()`` returns
            # False at the next call.
            if fetched is not None:
                accounts, tag_hints = fetched
                # ``tag_hints is None`` when the payload omits the
                # field (older server pre-PR #288) — pass-through to
                # ``replace`` keeps the prior hint map intact instead
                # of clobbering it with an empty dict (PR #290
                # round-2 review).
                cache.replace(accounts=accounts, tag_hints=tag_hints)
        server_accounts_by_provider = cache.provider_accounts()
        server_account_tag_hints = cache.provider_tag_hints()
    except Exception as _e:
        # Defensive: never let identity-fetch failures kill collection.
        logging.debug(f"identity-hint fetch skipped: {_e}")

    # Per-sidecar block-counter for the next /fleet/credentials/manifest
    # call (PR #288). Reset at the start of each ``run_collection`` so
    # the counter reflects "this cycle's block queue" only.
    blocked_origins_this_cycle: list[dict[str, str]] = []

    all_metrics: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    error_count = 0

    if providers is None:
        # No instructions yet (cold start) — collect everything enabled in config
        enabled_providers = config.get("providers", ["all"])
    elif not providers:
        # Empty list = pure heartbeat. Skip collection; the caller still pushes
        # an empty payload to /fleet/ingest so the server can deliver triggers.
        return [], [], 0
    else:
        enabled_providers = providers

    registry_providers = __REGISTRY__.get("providers", {})
    bootstrap_days = int(os.getenv("SIDECAR_BOOTSTRAP_DAYS", "90"))

    for provider_id, provider_config in registry_providers.items():
        if "all" not in enabled_providers and provider_id not in enabled_providers:
            continue
        try:
            logging.info(f"  [{provider_id}] collecting...")
            metrics, blocked = GenericCollector.collect_provider(
                provider_id, provider_config, account_label_hints=server_account_tag_hints
            )
            blocked_origins_this_cycle.extend(blocked)
            # Mirror the server's token-only predicate (fleet.py:118) so the
            # log lines line up with what the ingest endpoint actually does.
            token_cards = sum(
                1
                for c in metrics
                if c.get("remaining") == "Token" and c.get("unit") in ("oauth", "api_key", "cookie")
            )
            quota_cards = len(metrics) - token_cards
            if quota_cards and token_cards:
                logging.info(
                    f"  [{provider_id}] {quota_cards} quota card(s), {token_cards} token card(s)"
                )
            elif quota_cards:
                logging.info(f"  [{provider_id}] {quota_cards} quota card(s)")
            elif token_cards:
                logging.info(
                    f"  [{provider_id}] pushed {token_cards} token card(s) (server fetches quota)"
                )
            else:
                logging.info(f"  [{provider_id}] no data")
            all_metrics.extend(metrics)

            # Events block. Per-account iteration (issue #272). Resolve the host's
            # local identity first, then intersect with the server's per-
            # account list. We never iterate a server-side account that
            # doesn't match our local discovery — those belong to other
            # sidecar hosts, and stamping events under them would leak
            # another user's account_id into our event stream.
            if _events_enabled and _watermark is not None and provider_id in _EVENT_PROVIDERS:
                local_account_id = _LEGACY_EVENT_ACCOUNT_DISCOVERY[provider_id]()
                provider_accounts = server_accounts_by_provider.get(provider_id) or []

                # Silent-listener for events (PR #290 follow-up — the
                # token-card path has had this since #288, but events
                # shipped under "default" with no surface in the
                # Untagged Credentials dialog). Apply the same hint /
                # block pattern as the token-card branch above, so a
                # provider like MiniMax whose quota gauge lives at the
                # operator's chosen account_id (not "default") merges
                # with the sidecar's event stream the moment the
                # operator tags the origin via the dashboard.
                #
                # PR #318 round-2 review (Hermes): the branch-level
                # ``event_hint`` here only consults the iterating
                # provider's own hint bucket — NOT every canonical
                # provider that ``_OC_CANONICAL_MAP`` can retag events
                # to. The earlier dual-key walk spread the MiniMax
                # identity to unrelated opencode sub-providers
                # (``opencode-openai``, ``opencode-anthropic``, ...) by
                # stamping ``scoped_accounts = [event_hint]`` for the
                # whole iteration. The per-event ``canonical_hints``
                # forwarding in ``parse_opencode_events`` already
                # closes the card-split on its own (it applies the
                # canonical provider's hint to events that actually
                # retag to that provider), so this branch just needs
                # to keep iterating under the local identity — events
                # that don't retag continue to ship under "default".
                event_origin = credential_origin_for_provider(provider_id)
                event_hint = server_account_tag_hints.get(provider_id, {}).get(event_origin)

                # PR #318 round-2 re-review (Hermes): the evidence gate below
                # used a proxy (``scoped_accounts == [local or "default"]``
                # + ``event_hint is None``) that was ALSO true for the two
                # *resolved* branches whenever ``local_account_id`` was
                # truthy — a host whose identity the server already knew
                # (branch 1) or whose local identity we chose over
                # cross-host attribution (branch 2) still posted a phantom
                # Untagged entry + "shipping under 'default'" warning even
                # though events shipped under the resolved account.
                # Track untaggedness explicitly instead: only the final
                # else-arm (no server match, no local identity, no hint)
                # is genuinely unresolved, and its events always ship under
                # the legacy ``[local or "default"]`` sentinel — which the
                # warning log below accurately describes.
                untagged = False

                if provider_accounts and local_account_id and local_account_id in provider_accounts:
                    # Best case: server knows about us, and our local
                    # identity matches one of its rows.
                    scoped_accounts = [local_account_id]
                elif local_account_id and local_account_id != "default":
                    # Server has rows, none of which are us (e.g. local
                    # is some-other-id but the server registered
                    # ``alice@example.com`` only). Falling back to
                    # ``provider_accounts[0]`` would attribute our
                    # events to someone else's account (PR #283 review).
                    # Stamp with our local identity instead — better
                    # than the original #272 leak even if it ends up
                    # under "default".
                    if provider_accounts:
                        logging.debug(
                            "  [%s] local identity %r not in server accounts %r; "
                            "falling back to local identity rather than cross-host attribution",
                            provider_id,
                            local_account_id,
                            provider_accounts,
                        )
                    scoped_accounts = [local_account_id]
                elif event_hint:
                    # Local discovery returned the legacy "default"
                    # sentinel (no real identity on this host), but the
                    # operator has tagged this provider's events via
                    # the Untagged Credentials dialog (or the server's
                    # auto-hint fired for a single-account provider).
                    # Stamp with the operator's choice so the events
                    # land on the quota gauge instead of a standalone
                    # "default" card.
                    scoped_accounts = [event_hint]
                    logging.info(
                        f"  [{provider_id}] events stamped via server hint "
                        f"(origin={event_origin}) → account_id={event_hint}"
                    )
                else:
                    # Neither local discovery nor the server hint
                    # resolved a real account_id — ship under the
                    # legacy "default" sentinel so events don't
                    # disappear. Mark untagged so the evidence gate
                    # below can surface the origin: we only add the
                    # manifest entry when THIS provider actually
                    # extracted events this cycle (PR #318 round-2
                    # review, Hermes warning #3): reporting origins
                    # that no local credential backs produces a
                    # permanent Untagged entry that the operator can
                    # never resolve (no local artifact to map from),
                    # and the prune side never clears it.
                    untagged = True
                    scoped_accounts = [local_account_id or "default"]
                    logging.debug(
                        f"  [{provider_id}] events fall through to default "
                        f"(origin={event_origin}); reporting to manifest only "
                        f"if extraction produces events this cycle"
                    )

                # PR #318 round-2 review: gate the manifest entry on
                # evidence. Snapshot ``all_events`` length around the
                # call so we know whether THIS provider contributed any
                # events. Without this gate, the events branch reports
                # every iterating provider's origin unconditionally —
                # a host with no Claude/Codex artifacts publishes a
                # permanent ``provider:anthropic`` / ``provider:chatgpt``
                # Untagged entry for credentials it never had.
                pre_count = len(all_events)
                _extract_events_for_provider(
                    provider_id=provider_id,
                    account_ids=scoped_accounts,
                    watermark=_watermark,
                    bootstrap_days=bootstrap_days,
                    out_events=all_events,
                    server_account_tag_hints=server_account_tag_hints,
                )
                post_count = len(all_events)
                # Evidence gate (PR #318 round-2 W3): only surface an
                # Untagged origin when THIS provider contributed events.
                # PR #318 round-2 re-review W: gate on the explicit ``untagged``
                # flag rather than re-deriving it from ``scoped_accounts``
                # — the old proxy matched the resolved branches 1/2 too.
                if untagged and post_count > pre_count:
                    blocked_origins_this_cycle.append(
                        {
                            "provider_id": provider_id,
                            "credential_origin": event_origin,
                        }
                    )
                    logging.warning(
                        f"  [{provider_id}] events untagged (origin={event_origin}) — "
                        "neither local discovery nor the server hint resolved a real account_id; "
                        f"shipping {post_count - pre_count} event(s) under 'default' so they don't disappear. "
                        "Operator will see this in the fleet view's Untagged "
                        "Credentials panel and can tag it to land events on the "
                        "labeled quota card."
                    )

        except Exception as e:
            logging.error(f"  [{provider_id}] error: {e}")
            error_count += 1

    # Silent-listener manifest (PR #288): report every credential the
    # sidecar couldn't resolve this cycle, even if zero — silent cycles
    # let the server prune stale pending entries. The POST is gated on
    # the same INGEST_API_KEY HMAC scheme as /fleet/ingest.
    #
    # Gate on completed cycle (PR #290 round-2 review): when
    # ``error_count > 0`` a provider whose ``collect_provider`` raised
    # contributed nothing to ``blocked_origins_this_cycle``, so the
    # server's ``delete_stale`` would prune its pending row — the
    # operator's "Untagged" entry would silently disappear while the
    # credential is still unresolved. Skip the prune until the next
    # clean cycle.
    if blocked_origins_this_cycle:
        logging.info(
            f"  manifest: posting {len(blocked_origins_this_cycle)} blocked "
            f"credential origin(s) to /fleet/credentials/manifest"
        )

    def _consume_resolved_into_cache(
        resolved: dict[str, dict[str, str]],
    ) -> None:
        """Merge the server's just-returned tag map into the local cache.

        Lets the silent-listener loop close on the *same* cycle instead
        of waiting on the next ``/fleet/config`` round-trip — the
        operator's tag decision lands on the sidecar within seconds, not
        up to 10 minutes later (PR #290 round-2 review, Hermes
        suggestion #9).
        """
        nonlocal server_account_tag_hints
        # Merge into the live map so the *current* run's downstream
        # callers — if any — see the freshly-resolved tags without
        # needing to wait for the next ``/fleet/config``. The next
        # ``run_collection`` cycle's cache refresh will overwrite this
        # with whatever the server says at that point.
        merged: dict[str, dict[str, str]] = {
            k: dict(v) for k, v in server_account_tag_hints.items()
        }
        for pid, by_origin in resolved.items():
            if not isinstance(pid, str) or not isinstance(by_origin, dict):
                continue
            bucket = merged.setdefault(pid, {})
            for origin, account_id in by_origin.items():
                if (
                    isinstance(origin, str)
                    and isinstance(account_id, str)
                    and origin
                    and account_id
                ):
                    bucket[origin] = account_id
        server_account_tag_hints = merged
        # Also update the persistent cache so the next cycle doesn't
        # re-fetch what we just learned.
        try:
            cache = _get_credential_cache()
            cache.replace(tag_hints=server_account_tag_hints)
        except Exception as _cache_exc:  # pragma: no cover — defensive only
            logging.debug(f"manifest: cache.replace skipped ({_cache_exc})")

    if error_count > 0:
        logging.debug(
            f"manifest: posting on a partial cycle (error_count={error_count}); "
            "blocked origins from healthy providers still ship, "
            "providers that raised are absent from the manifest so their "
            "pending rows are NOT pruned"
        )
    # PR #290 round-2 review (Hermes thread Vha0): the prune is scoped
    # per-provider by what the manifest reports. The ``try/except``
    # above already filters ``blocked_origins_this_cycle`` to providers
    # whose ``collect_provider`` completed cleanly — so providers
    # that raised contribute no entries, and the server's
    # ``delete_stale`` never sees their keys. The ``if error_count > 0``
    # gate from round-1 was over-broad: it dropped healthy providers'
    # blocked origins while a single bad provider kept raising, leaving
    # the operator's Untagged surface blind. Post unconditionally so
    # the loop stays closed on every cycle.
    try:
        _post_credential_manifest(
            api_url=(os.environ.get("RUNWAY_API_URL") or config.get("api_url")),
            api_key=(os.environ.get("RUNWAY_API_KEY") or config.get("api_key") or ""),
            sidecar_id=get_hostname(),
            entries=blocked_origins_this_cycle,
            on_resolved=_consume_resolved_into_cache,
        )
    except Exception as _e:
        logging.debug(f"manifest: skipped ({_e})")

    return all_metrics, all_events, error_count


class DaemonRunner:
    """Owns the daemon lifecycle: collection loop, status tracking, threading."""

    def __init__(
        self,
        config: dict[str, Any],
        on_status_change: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        # Heartbeat: how often the sidecar pings the server for instructions.
        # The server (via /fleet/ingest's poll_providers field) is the cadence
        # authority — it tells the sidecar which providers are due. A short
        # heartbeat keeps refresh-button latency low; per-provider poll
        # intervals (set in the dashboard) decide how often each provider is
        # actually scraped.
        self._heartbeat: int = config.get("heartbeat_seconds", 60)
        # Staleness threshold for the "warn if no cycle in 2× this" check below.
        # Not a polling cadence — the server controls that via /fleet/ingest's
        # poll_providers response field.
        self._staleness_threshold: int = 900
        self.on_status_change = on_status_change

        # Readable state attributes
        self.last_cycle_at: float | None = None
        self.last_metrics_count: int = 0
        self.last_http_code: int | None = None
        self.last_error: str | None = None

        # Internal state flags
        self._status_reason: str = "starting"  # "starting"|"success"|"queued"|"error"|"paused"
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._trigger_event = threading.Event()  # set to skip the inter-cycle sleep
        self._paused = False
        self._cycle_running = False  # guard against concurrent run_once() calls
        self._next_poll_providers: list[str] | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Status property (computed)
    # ------------------------------------------------------------------

    @property
    def status(self) -> str:
        """Return one of: 'starting' | 'ok' | 'warn' | 'err' | 'paused'."""
        with self._lock:
            reason = self._status_reason

        if reason == "starting":
            return "starting"
        if reason == "paused":
            return "paused"
        if reason == "error":
            return "err"
        if reason == "queued":
            return "warn"
        # reason == "success"
        # Check staleness: warn if last cycle was more than 2× threshold ago
        if self.last_cycle_at is not None:
            age = time.time() - self.last_cycle_at
            if age > 2 * self._staleness_threshold:
                return "warn"
        return "ok"

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def run_once(self, providers: list[str] | None = None) -> bool:
        """Run one collection+ingest cycle synchronously. Returns True on success."""
        with self._lock:
            if self._cycle_running:
                logging.debug("run_once: cycle already in progress, skipping")
                return False
            self._cycle_running = True
        try:
            return self._run_once_impl(providers=providers)
        finally:
            with self._lock:
                self._cycle_running = False

    def _run_once_impl(self, providers: list[str] | None = None) -> bool:
        """Inner implementation of run_once (called only when no cycle is running)."""
        api_url = self._config["api_url"]
        api_key = self._config["api_key"]

        try:
            if providers is None:
                logging.info("Starting full collection cycle...")
            elif not providers:
                logging.debug("Heartbeat — pinging server for instructions")
            else:
                logging.info(f"Starting targeted collection for: {providers}...")

            metrics, events, collection_errors = run_collection(self._config, providers=providers)

            os_platform = f"{platform.system()}/{platform.release()}"
            sidecar_version = self._config.get("sidecar_version") or _SIDECAR_VERSION
            # Whether this build can replace its own binary (frozen, non-Docker).
            # From-source / Docker report False so the server won't offer a
            # self-update push. None on any failure → server stays permissive.
            try:
                from scripts.sidecar_pkg.self_update import self_update_supported

                self_update_capable: bool | None = self_update_supported()
            except Exception:
                self_update_capable = None

            # Try to flush queue first
            queue_flush(api_url, api_key, stop_event=self._stop_event)

            # Spec §7.3: cap each POST at 1000 events. Bootstrap (90-day backfill)
            # commonly produces 5k–50k events; a single payload would exceed the
            # server's 8 MB body limit. Send the first batch with metrics +
            # heartbeat fields; subsequent batches are events-only.
            EVENT_BATCH_SIZE = 1000
            event_batches = (
                [events[i : i + EVENT_BATCH_SIZE] for i in range(0, len(events), EVENT_BATCH_SIZE)]
                if events
                else [[]]
            )

            success = True
            result: Any = None
            code: int = 0
            ingest_url = f"{api_url.rstrip('/')}/api/v1/fleet/ingest"
            for batch_idx, event_batch in enumerate(event_batches):
                first_batch = batch_idx == 0
                payload = {
                    "provider": f"sidecar-{get_hostname()}",
                    "metrics": metrics if first_batch else [],
                    "events": event_batch,
                    "sidecar_id": get_hostname(),
                    "sidecar_version": sidecar_version,
                    "os_platform": os_platform,
                    "self_update_capable": self_update_capable if first_batch else None,
                    "collection_errors": collection_errors if first_batch else 0,
                    "last_log_lines": (_tail_log(20) if not providers else [])
                    if first_batch
                    else [],
                }
                success, result, code = http_post_signed_with_retry(
                    ingest_url,
                    payload,
                    api_key,
                    max_attempts=self._config.get("retry_attempts", 3),
                    backoff_seconds=self._config.get("retry_backoff_seconds", 5),
                    stop_event=self._stop_event,
                )
                if not success:
                    break  # don't keep firing batches if the server is rejecting them
                if len(event_batches) > 1:
                    logging.info(
                        f"  sent batch {batch_idx + 1}/{len(event_batches)} "
                        f"({len(event_batch)} events)"
                    )

            with self._lock:
                self.last_cycle_at = time.time()
                self.last_metrics_count = len(metrics)
                self.last_http_code = code

            if success:
                if metrics or events:
                    logging.info(f"Successfully sent {len(metrics)} metrics, {len(events)} events")
                else:
                    logging.debug("Heartbeat successful")

                # Advance watermark for successfully pushed events.
                if events:
                    try:
                        from scripts.sidecar_pkg.event_watermark import EventWatermark

                        wm = EventWatermark(
                            Path(
                                os.path.expanduser("~/.config/runway-sidecar/event-watermark.json")
                            )
                        )
                        for ev in events:
                            ts_str = ev.get("ts")
                            if ts_str:
                                try:
                                    ts = datetime.datetime.fromisoformat(
                                        ts_str.replace("Z", "+00:00")
                                    )
                                    wm.advance(ev["provider_id"], ev["account_id"], ts)
                                except Exception:
                                    logging.debug(
                                        "Failed to advance event watermark", exc_info=True
                                    )
                    except Exception as e:
                        logging.warning(f"Failed to advance event watermark: {e}")

                with self._lock:
                    self.last_error = None
                    self._status_reason = "success"
                self._fire_status_change()

                if isinstance(result, dict):
                    # Store server-provided identity hints (for anonymous collectors)
                    identities = result.get("identities")
                    if identities:
                        global _ACCOUNT_IDENTITIES
                        _ACCOUNT_IDENTITIES.update(identities)
                        logging.debug(f"Server provided identities: {identities}")

                    # Log reset_anchors for visibility (Phase 6)
                    reset_anchors = result.get("reset_anchors")
                    if reset_anchors:
                        global _GLOBAL_RESET_ANCHORS
                        _GLOBAL_RESET_ANCHORS.update(reset_anchors)
                        logging.debug(f"Server reset_anchors: {reset_anchors}")

                    # Update channel the dashboard wants this sidecar to track
                    # for the (notify-only) update check.
                    update_channel = result.get("sidecar_update_channel")
                    if update_channel:
                        global _UPDATE_CHANNEL
                        if update_channel != _UPDATE_CHANNEL:
                            logging.debug(f"Server update channel: {update_channel}")
                        _UPDATE_CHANNEL = update_channel

                    # Fleet-wide auto-update flag from the dashboard. A sidecar's
                    # explicit local `auto_update` config still wins (see
                    # _auto_update_enabled); this only sets the server side.
                    global _AUTO_UPDATE_SERVER
                    server_auto = bool(result.get("sidecar_auto_update", False))
                    if server_auto != _AUTO_UPDATE_SERVER:
                        logging.debug(f"Server auto-update flag: {server_auto}")
                    _AUTO_UPDATE_SERVER = server_auto

                    # One-shot admin "Update now" push: self-install immediately,
                    # regardless of the auto-update toggle. self_update's own
                    # frozen/Docker/single-flight guards make this safe and a
                    # no-op where it doesn't apply.
                    if result.get("update_now"):
                        logging.info("Server pushed an update; installing now")
                        try:
                            from scripts.sidecar_pkg.self_update import self_update

                            self_update(
                                _SIDECAR_VERSION,
                                os.environ.get("RUNWAY_UPDATE_CHANNEL") or _UPDATE_CHANNEL,
                            )
                        except Exception:
                            logging.warning("Pushed self-update failed", exc_info=True)

                    # The server is the cadence authority. Each ingest response
                    # tells the sidecar exactly which providers to collect on
                    # the *next* heartbeat tick:
                    #   - trigger=true       → collect everything (refresh button)
                    #   - poll_providers=[…] → collect just those (per-provider due)
                    #   - poll_providers=[]  → pure heartbeat, no collection
                    if result.get("trigger"):
                        logging.info(
                            "Remote trigger received — collecting everything on next heartbeat"
                        )
                        self._next_poll_providers = None  # None = full collection
                        self._trigger_event.set()
                    else:
                        poll_providers = result.get("poll_providers")
                        if poll_providers is not None:
                            self._next_poll_providers = poll_providers
                            if poll_providers:
                                logging.info(f"Server requested targeted poll: {poll_providers}")

                return True

            # Check for clock skew error (400 timestamp_expired). Note that
            # `result["detail"]` is a dict only for the structured clock-skew
            # response — validation errors return a plain string there.
            detail = result.get("detail") if isinstance(result, dict) else None
            if (
                code == 400
                and isinstance(detail, dict)
                and detail.get("error") == "timestamp_expired"
            ):
                skew = detail.get("skew_seconds", "?")
                logging.error("=" * 60)
                logging.error("⚠️  CLOCK SKEW DETECTED — REQUEST REJECTED")
                logging.error(f"Server reported skew of {skew} seconds.")
                logging.error("Please check NTP sync on this machine.")
                logging.error("=" * 60)
            else:
                logging.error(f"Failed to send metrics (HTTP {code}): {result}")

            # A code-0 failure (no HTTP response at all) combined with a missing
            # extraction dir means the *local* frozen runtime is corrupted, not
            # that the server is unreachable — retrying forever can never fix
            # that. Exit hard so systemd's Restart=always re-extracts a clean
            # runtime. Runs on a background daemon thread, so sys.exit() alone
            # would only unwind this thread; os._exit() terminates the process.
            if code == 0 and _frozen_runtime_missing():
                logging.critical(
                    "Frozen runtime extraction dir is gone (%s) — exiting so "
                    "the service supervisor can restart with a clean unpack.",
                    getattr(sys, "_MEIPASS", "?"),
                )
                os._exit(70)  # EX_SOFTWARE

            # Only queue metrics payloads; heartbeats don't need to be queued
            if metrics or events:
                queue_push(payload)

            with self._lock:
                self.last_error = str(result)
                # "error" if we got a real HTTP response (non-2xx), "queued" for
                # network-level failures (no connectivity, code 0)
                self._status_reason = "error" if code > 0 else "queued"
            self._fire_status_change()
            return False

        except Exception as e:
            logging.error(f"Unexpected error in collection loop: {e}")
            with self._lock:
                self.last_cycle_at = time.time()
                self.last_error = str(e)
                self._status_reason = "error"
            self._fire_status_change()
            return False

    def start(self) -> None:
        """Spawn a background daemon thread running the collection loop."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="DaemonRunner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to exit and join the background thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                logging.warning("DaemonRunner thread did not exit within 30s")
            self._thread = None

    def pause(self) -> None:
        """Temporarily skip collection cycles."""
        with self._lock:
            self._paused = True
            self._status_reason = "paused"
        self._fire_status_change()

    def resume(self) -> None:
        """Unpause collection cycles."""
        with self._lock:
            self._paused = False
            # Restore status based on last cycle result; treat as starting if no cycle yet
            if self.last_cycle_at is None:
                self._status_reason = "starting"
            else:
                # Restore to "success" so the status property can recompute ok/warn from
                # last_cycle_at staleness rather than remaining stuck on "paused"
                self._status_reason = "success"
        self._fire_status_change()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fire_status_change(self) -> None:
        """Invoke on_status_change callback if provided."""
        if self.on_status_change is not None:
            self.on_status_change(self.status)

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep for up to *seconds*, but wake immediately on stop or trigger."""
        deadline = time.time() + seconds
        while not self._stop_event.is_set() and not self._trigger_event.is_set():
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._stop_event.wait(timeout=min(remaining, 1.0))
        self._trigger_event.clear()

    def _loop(self) -> None:
        """Background thread: heartbeat the server until stopped.

        Each iteration is short (default 60s). What runs on each heartbeat
        is decided by the server's previous /fleet/ingest response:
          * None  → cold-start full collection
          * []    → heartbeat only (push empty payload, get instructions back)
          * [p1…] → collect just those providers (per-provider cadence due)
          * trigger=true also resets to None and wakes the sleep early.
        """
        while not self._stop_event.is_set():
            if self._paused:
                # While paused, sleep in short bursts so stop() is responsive
                self._stop_event.wait(timeout=1)
                continue

            with self._lock:
                providers = self._next_poll_providers

            self.run_once(providers=providers)

            if self._stop_event.is_set():
                break

            # Short heartbeat sleep — wakes early on stop or remote trigger.
            self._interruptible_sleep(self._heartbeat)

        logging.info("DaemonRunner loop exited.")


def main():
    parser = argparse.ArgumentParser(description="Runway Sidecar")
    parser.add_argument("--config", help="Path to config.json")
    parser.add_argument("--run-once", action="store_true", help="Run once and exit")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon (default)")
    parser.add_argument(
        "--self-update",
        "--update",
        dest="self_update",
        action="store_true",
        help="Download, verify and install the latest build, then exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_SIDECAR_VERSION}",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_level", "INFO"), config.get("log_file_enabled", True))

    # Manual self-update runs one synchronous download→verify→install and exits.
    # Handled before write_pid_file() so a running daemon's PID lock can't block
    # an explicit `--self-update` invocation.
    if args.self_update:
        from scripts.sidecar_pkg.self_update import self_update

        channel = os.environ.get("RUNWAY_UPDATE_CHANNEL") or _UPDATE_CHANNEL
        ok = self_update(_SIDECAR_VERSION, channel, restart=False)
        sys.exit(0 if ok else 1)

    # Tri-state local override: explicit true/false wins over the server flag;
    # absent (None) defers to the server's fleet-wide setting.
    global _AUTO_UPDATE_LOCAL
    local_auto = config.get("auto_update")
    _AUTO_UPDATE_LOCAL = None if local_auto is None else bool(local_auto)

    if not write_pid_file():
        sys.exit(1)

    setup_signal_handlers()
    atexit.register(cleanup)

    api_url = config["api_url"]

    logging.info(f"Sidecar started for {api_url}")

    global _daemon_running
    _daemon_running = True

    runner = DaemonRunner(config)

    if args.run_once:
        runner.run_once()
    else:
        runner.start()

        # Background update check. Logs a warning when a newer build is
        # available on the active channel; when `auto_update` is on (frozen
        # builds only), it also self-installs. Channel priority:
        # RUNWAY_UPDATE_CHANNEL env override > server-synced > inferred.
        update_thread = None
        try:
            from scripts.sidecar_pkg.update_check import UpdateCheckThread

            def _channel_getter() -> str | None:
                return os.environ.get("RUNWAY_UPDATE_CHANNEL") or _UPDATE_CHANNEL

            def _maybe_self_update(_desc: str) -> None:
                if not _auto_update_enabled():
                    return
                from scripts.sidecar_pkg.self_update import self_update

                self_update(_SIDECAR_VERSION, _channel_getter())

            update_thread = UpdateCheckThread(
                _SIDECAR_VERSION,
                channel_getter=_channel_getter,
                on_update_available=_maybe_self_update,
            )
            update_thread.start()
        except Exception:
            logging.debug("Update-check thread not started", exc_info=True)

        try:
            # Block until signal handler sets _daemon_running = False
            while _daemon_running:
                time.sleep(1)
        finally:
            if update_thread is not None:
                update_thread.stop()
            runner.stop()

    logging.info("Sidecar stopping...")


if __name__ == "__main__":
    main()
