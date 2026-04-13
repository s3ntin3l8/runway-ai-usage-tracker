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
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request

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
                {"type": "env", "variable": "OPENROUTER_API_KEY", "mapping": {"value": "api_key"}}
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
            "name": "GitHub API",
            "icon": "\ud83d\udc19",
            "rules": [
                {"type": "env", "variable": "GITHUB_TOKEN", "mapping": {"value": "api_key"}},
                {
                    "type": "file",
                    "paths": ["{{CONFIG_DIR:usage-tracker}}/github_oauth.json"],
                    "format": "json",
                    "mapping": {"access_token": "api_key"},
                },
                {
                    "type": "file",
                    "paths": ["~/.config/gh/hosts.yml", "{{CONFIG_DIR:gh}}/hosts.yml"],
                    "format": "yaml",
                    "mapping": {"github.com.oauth_token": "api_key"},
                },
                {
                    "type": "windows_credential",
                    "target": "github.com",
                    "mapping": {"value": "api_key"},
                },
            ],
        },
        "gemini": {
            "name": "Gemini API",
            "icon": "\ud83d\udd35",
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
                }
            ],
        },
        "chatgpt": {
            "name": "ChatGPT Codex",
            "icon": "\ud83d\udcac",
            "rules": [
                {
                    "type": "env",
                    "variable": "CHATGPT_OAUTH_TOKEN",
                    "mapping": {"value": "oauth_token"},
                },
                {
                    "type": "file",
                    "paths": ["~/.codex/auth.json", "{{CONFIG_DIR:codex}}/auth.json"],
                    "format": "json",
                    "mapping": {"tokens.access_token": "oauth_token"},
                },
                {
                    "type": "cookie",
                    "domains": ["chatgpt.com"],
                    "name": "__Secure-next-auth.session-token",
                    "mapping": {"value": "cookie___Secure-next-auth.session-token"},
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
                {
                    "type": "sqlite",
                    "paths": [
                        "~/.local/share/opencode/opencode.db",
                        "{{DATA_DIR:opencode}}/opencode.db",
                    ],
                    "queries": [
                        {
                            "name": "Usage (5h/7d/30d)",
                            "query": "SELECT SUM(json_extract(data, '$.cost')), COUNT(*) FROM message WHERE time_created > ? AND json_valid(data) AND json_extract(data, '$.role') = 'assistant'",
                            "windows": {"5h": 18000, "week": 604800, "month": 2592000},
                            "limits": {"5h": 12.0, "week": 30.0, "month": 60.0},
                        }
                    ],
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
                        "~/.antigravity/state/quota.json",
                        "{{DATA_DIR:antigravity}}/state/quota.json",
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
    "interval_seconds": 1800,
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
    """Get the sidecar configuration directory."""
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
            "interval_seconds": 1800,
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

    # Validate required fields
    missing = [f for f in REQUIRED_CONFIG_FIELDS if f not in config or not config[f]]
    if missing:
        print(f"ERROR: Missing required config fields: {', '.join(missing)}")
        print(f"Config file: {config_file}")
        sys.exit(1)

    # Apply defaults for optional fields
    for key, value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = value

    return config


# --- Logging Setup ---


def setup_logging(log_level: str, file_enabled: bool) -> None:
    """Configure logging with console and optional file output."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if file_enabled:
        ensure_dirs()
        log_path = get_log_path()
        file_handler = logging.FileHandler(log_path, mode="a")
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


def queue_flush(api_url: str, api_key: str) -> int:
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
        try:
            with open(queue_file) as f:
                lines = f.readlines()

            failed_lines = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                    payload = entry.get("payload", {})

                    success, _ = http_post_signed_with_retry(target_url, payload, api_key)

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
        return False, str(e), 500


def http_post_signed_with_retry(
    url: str,
    data: dict[str, Any],
    api_key: str,
    max_attempts: int = 3,
    backoff_seconds: int = 5,
) -> tuple[bool, Any, int]:
    """POST with exponential backoff retry."""
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
            time.sleep(wait)

    return False, last_error, last_code


def human_delta(target_dt):
    """Format datetime as human-readable delta."""
    if not target_dt:
        return "—"
    now = datetime.datetime.now(datetime.UTC)
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
                    conn = sqlite3.connect(f"file:{str(target['path'])}?mode=ro&uri=1", uri=True)
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
                                conn.close()
                                return val
                    else:  # Firefox
                        cursor.execute(
                            "SELECT value FROM moz_cookies WHERE host LIKE ? AND name = ?",
                            (f"%{domain}%", name),
                        )
                        row = cursor.fetchone()
                        if row:
                            val = row[0]
                            conn.close()
                            return val
                    conn.close()
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


# --- Generic Collector Engine ---


class GenericCollector:
    """Orchestrates data collection based on registry rules."""

    @staticmethod
    def get_nested(data: Any, key_path: str) -> Any:
        """Get nested value from dict using dot notation or list of keys."""
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
                            break

            # 6. Specialized: SQLite (OpenCode)
            elif rule_type == "sqlite":
                for path_str in rule.get("paths", []):
                    path = resolve_path(path_str)
                    if path.exists():
                        try:
                            conn = sqlite3.connect(str(path))
                            cursor = conn.cursor()
                            now = datetime.datetime.now(datetime.UTC)
                            hostname = get_hostname()

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
                                            "metadata": {
                                                "used": used,
                                                "count": count,
                                                "window": window_name,
                                                "hostname": hostname,
                                            },
                                        }
                                    )
                            conn.close()
                        except Exception as e:
                            logging.debug(f"SQLite error for {provider_id}: {e}")

            # 7. Specialized: JSON Data (Antigravity)
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
                                    if reset_ts
                                    else None
                                )
                                results.append(
                                    {
                                        "service_name": f"AG: {m_name}",
                                        "icon": icon,
                                        "remaining": f"{rem:.1f}%",
                                        "unit": "remaining",
                                        "reset": human_delta(reset_at),
                                        "health": "good" if rem > 30 else "warning",
                                        "pace": "Stable",
                                        "detail": f"{m_name} [Sidecar]",
                                        "data_source": "local",
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
                                        "metadata": {"used": pct_used, "resets_at": reset_ts},
                                    }
                                )

                            # Context / Tokens
                            ctx = data.get("context_window", {})
                            if ctx:
                                tokens = ctx.get("total_input_tokens", 0) + ctx.get(
                                    "total_output_tokens", 0
                                )
                                max_t = ctx.get("max_tokens", 200000)
                                results.append(
                                    {
                                        "service_name": "Claude (Session Tokens)",
                                        "icon": "🪙",
                                        "remaining": f"{tokens:,}",
                                        "unit": f"/ {max_t:,}",
                                        "reset": data.get("model", {}).get(
                                            "display_name", "Sonnet"
                                        ),
                                        "health": "good",
                                        "pace": "Active",
                                        "detail": f"{tokens:,} tokens [Sidecar]",
                                        "data_source": "local",
                                    }
                                )
                        except Exception:
                            pass

        # If tokens were extracted, add a hidden token card
        if tokens:
            results.append(
                {
                    "service_name": name,
                    "icon": icon,
                    "remaining": "Token",
                    "unit": "oauth" if "oauth_token" in tokens else "api_key",
                    "reset": "—",
                    "health": "good",
                    "pace": "Token",
                    "detail": "[Token Extracted] [Sidecar]",
                    "data_source": "token_extracted",
                    "metadata": {**tokens, "provider_id": provider_id},
                }
            )

        return results


# --- Main Loop ---


def run_collection(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Run collection for all enabled providers."""
    all_metrics = []
    enabled_providers = config.get("providers", ["all"])

    registry_providers = __REGISTRY__.get("providers", {})

    for provider_id, provider_config in registry_providers.items():
        if "all" in enabled_providers or provider_id in enabled_providers:
            try:
                metrics = GenericCollector.collect_provider(provider_id, provider_config)
                all_metrics.extend(metrics)
            except Exception as e:
                logging.error(f"Collector error for {provider_id}: {e}")

    return all_metrics


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
    api_key = config["api_key"]
    interval = config.get("interval_seconds", 1800)

    logging.info(f"Sidecar started for {api_url} (Interval: {interval}s)")

    global _daemon_running
    _daemon_running = True

    while _daemon_running:
        try:
            logging.info("Starting collection cycle...")
            metrics = run_collection(config)

            if metrics:
                payload = {"provider": f"sidecar-{get_hostname()}", "metrics": metrics}

                # Try to flush queue first
                queue_flush(api_url, api_key)

                # Send fresh metrics
                success, result, code = http_post_signed_with_retry(
                    f"{api_url.rstrip('/')}/api/v1/fleet/ingest",
                    payload,
                    api_key,
                    max_attempts=config.get("retry_attempts", 3),
                    backoff_seconds=config.get("retry_backoff_seconds", 5),
                )

                if success:
                    logging.info(f"Successfully sent {len(metrics)} metrics")
                else:
                    # Check for clock skew error (400 timestamp_expired)
                    if (
                        code == 400
                        and isinstance(result, dict)
                        and result.get("detail", {}).get("error") == "timestamp_expired"
                    ):
                        skew = result.get("detail", {}).get("skew_seconds", "?")
                        logging.error("=" * 60)
                        logging.error("⚠️  CLOCK SKEW DETECTED — REQUEST REJECTED")
                        logging.error(f"Server reported skew of {skew} seconds.")
                        logging.error("Please check NTP sync on this machine.")
                        logging.error("=" * 60)
                    else:
                        logging.error(f"Failed to send metrics (HTTP {code}): {result}")
                    queue_push(payload)
            else:
                logging.info("No metrics collected in this cycle")

        except Exception as e:
            logging.error(f"Unexpected error in collection loop: {e}")

        if args.run_once:
            break

        # Wait for next interval
        for _ in range(interval):
            if not _daemon_running:
                break
            time.sleep(1)

    logging.info("Sidecar stopping...")


if __name__ == "__main__":
    main()
