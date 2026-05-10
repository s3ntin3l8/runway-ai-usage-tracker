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
import struct
import subprocess
import sys
import threading
import time
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

# --- INJECTED REGISTRY ---
__REGISTRY__ = {
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
                {
                    "type": "file",
                    "paths": ["~/.codex/auth.json", "{{CONFIG_DIR:codex}}/auth.json"],
                    "format": "json",
                    "mapping": {"tokens.access_token": "oauth_token"},
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
                {
                    "type": "env",
                    "variable": "KIMI_AUTH_TOKEN",
                    "mapping": {"value": "session_cookie"},
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
                    "type": "file_json_data",
                    "paths": [
                        "{{DATA_DIR:antigravity}}/state/quota.json",
                        # Linux: ~/.local/share/antigravity/state/quota.json
                        # macOS: ~/Library/Application Support/antigravity/state/quota.json
                        # Windows: path unconfirmed — LSP probing is primary on Windows
                    ],
                }
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
    "interval_seconds": 900,
    "heartbeat_seconds": 60,
    "providers": ["all"],
    "retry_attempts": 3,
    "retry_backoff_seconds": 5,
    "queue_max_size_mb": 10,
    "log_level": "INFO",
    "log_file_enabled": True,
}

REQUIRED_CONFIG_FIELDS = ["api_url", "api_key"]

# Global state for daemon mode
_daemon_running = False
_pid_file_path: Path | None = None
_hostname: str | None = None
_windows_cred_cache: dict = {}
_windows_cred_ttl_seconds: int = 300


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
    """Get cached hostname or call gethostname() once."""
    global _hostname
    if _hostname is None:
        _hostname = socket.gethostname()
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
            "interval_seconds": 900,
            "heartbeat_seconds": 60,
            "providers": ["all"],
            "retry_attempts": 3,
            "retry_backoff_seconds": 5,
            "queue_max_size_mb": 10,
            "log_level": "INFO",
            "log_file_enabled": True,
        }
        config_file.write_text(json.dumps(template, indent=2))
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


def write_pid_file() -> bool:
    """Write PID file. Returns False if already running."""
    global _pid_file_path
    _pid_file_path = get_pid_file_path()

    # Check if already running
    if _pid_file_path.exists():
        try:
            old_pid = int(_pid_file_path.read_text().strip())
            # Check if process exists
            if platform.system() == "Windows":
                import ctypes

                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(1, False, old_pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    logging.error(f"Sidecar already running (PID: {old_pid})")
                    return False
            else:
                os.kill(old_pid, 0)  # Check if process exists
                logging.error(f"Sidecar already running (PID: {old_pid})")
                return False
        except (OSError, ValueError, ProcessLookupError):
            # Process not running, stale PID file
            pass

    _pid_file_path.write_text(str(os.getpid()))
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
    """Setup signal handlers for graceful shutdown."""
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal_handler)


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


def health_check(api_url: str, timeout: int = 5) -> bool:
    """Check if server is healthy before pushing."""
    try:
        req = request.Request(f"{api_url.rstrip('/')}/api/health", method="GET")
        with request.urlopen(req, timeout=timeout) as resp:
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
        with request.urlopen(req, timeout=15) as resp:
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
        import re

        match = re.search(r"{{CONFIG_DIR:([^}]+)}}", path_str)
        if match:
            app_name = match.group(1)
            path_str = path_str.replace(match.group(0), str(get_platform_config_dir(app_name)))

    if "{{DATA_DIR:" in path_str:
        import re

        match = re.search(r"{{DATA_DIR:([^}]+)}}", path_str)
        if match:
            app_name = match.group(1)
            path_str = path_str.replace(match.group(0), str(get_platform_data_dir(app_name)))

    return Path(path_str)


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
            pass
        return None

    # Windows decryption
    if system == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [
                    ("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(wintypes.BYTE)),
                ]

            crypt32 = ctypes.windll.crypt32
            blob_in = DATA_BLOB()
            blob_in.cbData = len(encrypted_value)
            blob_in.pbData = ctypes.cast(encrypted_value, ctypes.POINTER(wintypes.BYTE))
            blob_out = DATA_BLOB()
            if crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            ):
                buffer = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return buffer.decode("utf-8")
        except Exception:
            pass
        return None

    # Linux decryption
    try:
        try:
            return encrypted_value.decode("utf-8")
        except Exception:
            pass
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
        pass
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
            return []

    @staticmethod
    def get_cookie(domain, name):
        for target in BrowserCookieExtractor.get_all_paths():
            try:
                if target["type"] == "safari":
                    for c in BrowserCookieExtractor.parse_safari(target["path"]):
                        if domain in c["domain"] and c["name"] == name:
                            return c["value"]
                else:
                    with sqlite3.connect(
                        f"file:{str(target['path'])}?mode=ro&uri=1", uri=True
                    ) as conn:
                        cursor = conn.cursor()
                        if target["type"] == "chromium":
                            cursor.execute(
                                "SELECT encrypted_value FROM cookies WHERE host_key LIKE ? AND name = ?",
                                (f"%{domain}%", name),
                            )
                            row = cursor.fetchone()
                            if row:
                                val = decrypt_chromium_cookie(row[0], target["browser"])
                                if val:
                                    return val
                        else:  # Firefox
                            cursor.execute(
                                "SELECT value FROM moz_cookies WHERE host LIKE ? AND name = ?",
                                (f"%{domain}%", name),
                            )
                            row = cursor.fetchone()
                            if row:
                                return row[0]
            except Exception:
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
        pass
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
                pass
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
            pass

    return "default"


# --- Generic Collector Engine ---


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
    def collect_provider(provider_id: str, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Run all rules for a single provider and return metrics."""
        results = []
        tokens = {}

        name = config.get("name", provider_id)
        icon = config.get("icon", "❓")
        rules = config.get("rules", [])

        # Priority LSP probe for antigravity — runs before file-fallback rules
        if provider_id == "antigravity":
            lsp_cards = collect_antigravity_lsp(icon)
            if lsp_cards:
                logging.info(f"  [antigravity] LSP returned {len(lsp_cards)} card(s)")
                return lsp_cards  # skip file rules to avoid duplicates
            logging.info("  [antigravity] LSP probe found nothing, falling back to file")

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
                for path_str in rule.get("paths", []):
                    path = resolve_path(path_str)
                    if path.exists():
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
                    pass

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
                    pass

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
                                        pass

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

            elif rule_type == "file_json_data":
                for path_str in rule.get("paths", []):
                    path = resolve_path(path_str)
                    if path.exists():
                        try:
                            with open(path) as f:
                                data = json.load(f)
                            for m_name, usage in data.get("models", {}).items():
                                rem = usage.get("remaining_percent", 0.0)
                                reset_ts = usage.get("resets_at")
                                reset_at = (
                                    datetime.datetime.fromtimestamp(reset_ts, tz=datetime.UTC)
                                    if reset_ts is not None
                                    else None
                                )
                                results.append(
                                    {
                                        "service_name": m_name,
                                        "icon": icon,
                                        "remaining": f"{rem:.1f}%",
                                        "unit": "remaining",
                                        "reset": human_delta(reset_at),
                                        "health": "good" if rem > 30 else "warning",
                                        "pace": "Stable",
                                        "detail": f"{m_name} [Sidecar]",
                                        "data_source": "local",
                                        "provider_id": "antigravity",
                                        "account_label": None,
                                        "model_id": m_name,
                                        "used_value": round(100.0 - rem, 4),
                                        "limit_value": 100.0,
                                        "unit_type": "percent",
                                        "window_type": "session",
                                        "reset_at": reset_at.isoformat() if reset_at else None,
                                        "metadata": {
                                            "name": m_name,
                                            "remaining_percent": rem,
                                            "resets_at": reset_ts,
                                        },
                                    }
                                )
                        except Exception:
                            pass

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
                            pass

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

        # Post-process: propagate discovered account identity to all cards
        acc_id = tokens.get("account_id")
        acc_label = tokens.get("account_label")
        if acc_id or acc_label:
            for card in results:
                if acc_id and not card.get("account_id"):
                    card["account_id"] = acc_id
                if acc_label and not card.get("account_label"):
                    card["account_label"] = acc_label

        return results


# --- Antigravity LSP Probing ---


def _ag_detect_process_windows() -> dict[int, list[str]]:
    """Find Antigravity/Windsurf language server PID + CSRF tokens on Windows.

    Uses PowerShell instead of wmic because wmic /format:csv produces UTF-16LE
    output that Python misreads through subprocess text=True on most locales.
    """
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "Get-WmiObject Win32_Process | "
                "Where-Object {$_.CommandLine -like '*language_server*'} | "
                'ForEach-Object { "$($_.ProcessId)|$($_.CommandLine)" }',
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        procs: dict[int, list[str]] = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or "|" not in line or "language_server" not in line.lower():
                continue
            pid_str, _, cmdline = line.partition("|")
            try:
                pid = int(pid_str.strip())
            except ValueError:
                continue
            tokens = []
            for pattern in [
                r"--csrf_token\s+([a-f0-9-]+)",
                r"--extension_server_csrf_token\s+([a-f0-9-]+)",
            ]:
                m = re.search(pattern, cmdline)
                if m:
                    tokens.append(m.group(1))
            if tokens:
                procs[pid] = tokens
        return procs
    except Exception:
        return {}


def _ag_detect_process_unix() -> dict[int, list[str]]:
    """Find Antigravity/Windsurf language server PID + CSRF tokens on macOS/Linux."""
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid,command"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        procs: dict[int, list[str]] = {}
        for line in result.stdout.splitlines():
            if "language_server" not in line:
                continue
            pid_match = re.search(r"^\s*(\d+)", line)
            if not pid_match:
                continue
            pid = int(pid_match.group(1))
            tokens = []
            for pattern in [
                r"--csrf_token\s+([a-f0-9-]+)",
                r"--extension_server_csrf_token\s+([a-f0-9-]+)",
            ]:
                m = re.search(pattern, line)
                if m:
                    tokens.append(m.group(1))
            if tokens:
                procs[pid] = tokens
        return procs
    except Exception:
        return {}


def _ag_find_ports_windows(pid: int) -> list[int]:
    """Find listening TCP ports for PID on Windows via PowerShell Get-NetTCPConnection."""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Get-NetTCPConnection -State Listen -OwningProcess {pid} "
                "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ports = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                ports.append(int(line))
        return sorted(set(ports))
    except Exception:
        return []


def _ag_find_ports_unix(pid: int) -> list[int]:
    """Find listening TCP ports for PID on macOS/Linux via lsof.

    `-a` is critical: lsof joins selection flags with OR by default, so
    without it the output includes every TCP listening socket on the
    machine (e.g. the Runway server itself) — the LSP probe would then
    POST to those ports and the server logs would fill with 404s for
    /exa.language_server_pb.LanguageServerService/GetUserStatus.
    """
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ports = []
        for line in result.stdout.splitlines():
            m = re.search(r":(\d+)\s+\(LISTEN\)", line)
            if m:
                ports.append(int(m.group(1)))
        return sorted(set(ports))
    except Exception:
        return []


def _ag_probe_lsp(port: int, csrf: str, icon: str) -> list[dict[str, Any]]:
    """HTTP probe one port/token combo and return metric cards."""
    url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus"
    body = json.dumps(
        {"metadata": {"ideName": "antigravity", "extensionName": "antigravity"}}
    ).encode()
    req = request.Request(url, data=body, method="POST")
    req.add_header("X-Codeium-Csrf-Token", csrf)
    req.add_header("Connect-Protocol-Version", "1")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=1) as resp:
            data = json.loads(resp.read())
        return _ag_parse_lsp_response(data, icon)
    except Exception:
        return []


def _ag_parse_lsp_response(data: dict[str, Any], icon: str) -> list[dict[str, Any]]:
    """Parse LSP GetUserStatus response into metric cards."""
    results = []
    user_status = data.get("userStatus", {})
    email = user_status.get("email", "")
    plan_info = user_status.get("planStatus", {}).get("planInfo", {})
    plan = plan_info.get("planName", "Standard")

    for cfg in user_status.get("cascadeModelConfigData", {}).get("clientModelConfigs", []):
        quota = cfg.get("quotaInfo", {})
        rem_frac = quota.get("remainingFraction")
        if rem_frac is None:
            continue
        label = cfg.get("label", "Model")
        # Antigravity's LSP returns modelOrAlias either as a string
        # (e.g. "MODEL_PLACEHOLDER_M36", "MODEL_OPENAI_GPT_OSS_120B_MEDIUM",
        # "claude-sonnet-4-5-20251001") or as a dict {"model": "<id>"}. Internal
        # ids follow the convention `MODEL_*` (ALL_CAPS); real model ids are
        # lowercase-with-dashes. Fall back to the human-readable label
        # (e.g. "Gemini 3.1 Pro (Low)") when the candidate is an internal id.
        raw_model = cfg.get("modelOrAlias", label)
        candidate = (
            raw_model.get("model") or raw_model.get("name") or raw_model.get("alias")
            if isinstance(raw_model, dict)
            else raw_model
        )
        if not candidate or str(candidate).startswith("MODEL_"):
            candidate = None
        model_id = candidate or label
        rem_pct = float(rem_frac) * 100
        # resetTime can be either a Unix timestamp (int/float) or an ISO 8601
        # string like "2026-05-09T13:03:17Z" — the LSP has returned both.
        reset_ts = quota.get("resetTime")
        reset_dt = None
        if reset_ts is not None:
            try:
                reset_dt = datetime.datetime.fromtimestamp(float(reset_ts), tz=datetime.UTC)
            except (TypeError, ValueError):
                try:
                    reset_dt = datetime.datetime.fromisoformat(str(reset_ts).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    reset_dt = None
        reset_at = reset_dt.isoformat() if reset_dt else None
        results.append(
            {
                "service_name": label,
                "icon": icon,
                "remaining": f"{rem_pct:.1f}%",
                "unit": "capacity",
                "reset": human_delta(reset_dt),
                "pace": "Continuous",
                "health": "good" if rem_pct > 30 else "warning",
                "detail": f"{plan} | {email} [LSP]",
                "data_source": "local",
                "input_source": "sidecar",
                "provider_id": "antigravity",
                "account_id": email or "default",
                "account_label": email or None,
                "model_id": model_id,
                "used_value": round(100.0 - rem_pct, 4),
                "limit_value": 100.0,
                "unit_type": "percent",
                "window_type": "session",
                "reset_at": reset_at,
            }
        )

    name_map = {"GOOGLE_ONE_AI": "Google AI Credits", "ANTHROPIC_CREDIT": "Anthropic Credits"}
    for cred in user_status.get("userTier", {}).get("availableCredits", []):
        c_type = cred.get("creditType", "AI Credits")
        amount = str(cred.get("creditAmount", "0"))
        display = name_map.get(c_type, c_type.replace("_", " ").title())
        try:
            health = "good" if int(amount) > 100 else "warning"
        except ValueError:
            health = "warning"
        results.append(
            {
                "service_name": display,
                "icon": "💰",
                "remaining": amount,
                "unit": "credits",
                "reset": "Prepaid",
                "pace": "N/A",
                "health": health,
                "detail": f"{display} | {email} [LSP]",
                "data_source": "local",
                "input_source": "sidecar",
                "provider_id": "antigravity",
                "account_id": email or "default",
                "account_label": email or None,
                "model_id": None,
                "used_value": None,
                "limit_value": None,
                "unit_type": "credits",
                "window_type": "session",
                "reset_at": None,
            }
        )
    return results


def collect_antigravity_lsp(icon: str) -> list[dict[str, Any]]:
    """Probe local Antigravity/Windsurf language server. Returns cards or []."""
    is_win = platform.system() == "Windows"
    procs = _ag_detect_process_windows() if is_win else _ag_detect_process_unix()
    if not procs:
        logging.debug("  [antigravity] no language server process found")
        return []

    logging.info(f"  [antigravity] found {len(procs)} language server process(es), probing...")
    seen: set[str] = set()
    results = []

    for pid, tokens in procs.items():
        ports = _ag_find_ports_windows(pid) if is_win else _ag_find_ports_unix(pid)
        logging.debug(f"  [antigravity] pid={pid} ports={ports}")
        for port in ports:
            for csrf in tokens:
                for card in _ag_probe_lsp(port, csrf, icon):
                    key = f"{card['service_name']}_{card['remaining']}"
                    if key not in seen:
                        results.append(card)
                        seen.add(key)
    return results


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
    """Return the path to the OpenCode SQLite database, or None if not found."""
    candidates = [
        os.path.expanduser("~/.local/share/opencode/opencode.db"),
    ]
    for p in candidates:
        path = Path(p)
        if path.exists():
            return path
    return None


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
        from scripts.sidecar_pkg.event_extractors.anthropic import parse_anthropic_events
        from scripts.sidecar_pkg.event_extractors.chatgpt import parse_chatgpt_events
        from scripts.sidecar_pkg.event_extractors.gemini import parse_gemini_events
        from scripts.sidecar_pkg.event_extractors.opencode import parse_opencode_events
        from scripts.sidecar_pkg.event_watermark import EventWatermark

        _watermark = EventWatermark(
            Path(os.path.expanduser("~/.config/runway-sidecar/event-watermark.json"))
        )
        _events_enabled = True
    except Exception as _e:
        logging.warning(f"Event extraction unavailable: {_e}")
        _watermark = None
        _events_enabled = False

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
            metrics = GenericCollector.collect_provider(provider_id, provider_config)
            if metrics:
                logging.info(f"  [{provider_id}] {len(metrics)} card(s)")
            else:
                logging.info(f"  [{provider_id}] no data")
            all_metrics.extend(metrics)

            # --- Anthropic event extraction ---
            if provider_id == "anthropic" and _events_enabled and _watermark is not None:
                try:
                    account_id = discover_anthropic_email() or "default"
                    since = _watermark.last_pushed("anthropic", account_id) or (
                        datetime.datetime.now(datetime.UTC)
                        - datetime.timedelta(days=bootstrap_days)
                    )
                    log_paths = _discover_anthropic_log_paths()
                    if log_paths:
                        evts = parse_anthropic_events(log_paths, account_id=account_id, since=since)
                        if evts:
                            logging.info(
                                f"  [anthropic] {len(evts)} new event(s) since {since.isoformat()}"
                            )
                            all_events.extend(e.model_dump(mode="json") for e in evts)
                except Exception as e:
                    logging.warning(f"  [anthropic] event extraction error: {e}")

            # --- ChatGPT/Codex event extraction ---
            if provider_id == "chatgpt" and _events_enabled and _watermark is not None:
                try:
                    account_id = _codex_account_email() or "default"
                    since = _watermark.last_pushed("chatgpt", account_id) or (
                        datetime.datetime.now(datetime.UTC)
                        - datetime.timedelta(days=bootstrap_days)
                    )
                    log_paths = _discover_codex_log_paths()
                    if log_paths:
                        evts = parse_chatgpt_events(log_paths, account_id=account_id, since=since)
                        if evts:
                            logging.info(
                                f"  [chatgpt] {len(evts)} new event(s) since {since.isoformat()}"
                            )
                            all_events.extend(e.model_dump(mode="json") for e in evts)
                except Exception as e:
                    logging.warning(f"  [chatgpt] event extraction error: {e}")

            # --- Gemini event extraction ---
            if provider_id == "gemini" and _events_enabled and _watermark is not None:
                try:
                    account_id = _gemini_account_email() or "default"
                    since = _watermark.last_pushed("gemini", account_id) or (
                        datetime.datetime.now(datetime.UTC)
                        - datetime.timedelta(days=bootstrap_days)
                    )
                    log_paths = _discover_gemini_log_paths()
                    if log_paths:
                        evts = parse_gemini_events(log_paths, account_id=account_id, since=since)
                        if evts:
                            logging.info(
                                f"  [gemini] {len(evts)} new event(s) since {since.isoformat()}"
                            )
                            all_events.extend(e.model_dump(mode="json") for e in evts)
                except Exception as e:
                    logging.warning(f"  [gemini] event extraction error: {e}")

            # --- OpenCode event extraction ---
            if provider_id == "opencode" and _events_enabled and _watermark is not None:
                try:
                    db_path = _discover_opencode_db_path()
                    account_id = _opencode_account_email(db_path)
                    if db_path is not None:
                        since = _watermark.last_pushed("opencode", account_id) or (
                            datetime.datetime.now(datetime.UTC)
                            - datetime.timedelta(days=bootstrap_days)
                        )
                        evts = parse_opencode_events(db_path, account_id=account_id, since=since)
                        if evts:
                            logging.info(
                                f"  [opencode] {len(evts)} new event(s) since {since.isoformat()}"
                            )
                            all_events.extend(e.model_dump(mode="json") for e in evts)
                except Exception as e:
                    logging.warning(f"  [opencode] event extraction error: {e}")

        except Exception as e:
            logging.error(f"  [{provider_id}] error: {e}")
            error_count += 1

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
        # Retained for backward compat — older code paths read this. With the
        # heartbeat-driven model, the cadence is server-controlled, so this
        # value is no longer the primary loop period.
        self._interval: int = config.get("interval_seconds", 900)
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
        # Check staleness: warn if last cycle was more than 2× interval ago
        if self.last_cycle_at is not None:
            age = time.time() - self.last_cycle_at
            if age > 2 * self._interval:
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
            sidecar_version = self._config.get("sidecar_version", "unknown")

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
                                    pass
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
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_level", "INFO"), config.get("log_file_enabled", True))

    if not write_pid_file():
        sys.exit(1)

    setup_signal_handlers()
    atexit.register(cleanup)

    api_url = config["api_url"]
    interval = config.get("interval_seconds", 900)

    logging.info(f"Sidecar started for {api_url} (Interval: {interval}s)")

    global _daemon_running
    _daemon_running = True

    runner = DaemonRunner(config)

    if args.run_once:
        runner.run_once()
    else:
        runner.start()
        try:
            # Block until signal handler sets _daemon_running = False
            while _daemon_running:
                time.sleep(1)
        finally:
            runner.stop()

    logging.info("Sidecar stopping...")


if __name__ == "__main__":
    main()
