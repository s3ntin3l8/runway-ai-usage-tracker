#!/usr/bin/env python3
"""
Runway Sidecar - Token and Local Data Collector

Architecture:
- Extracts tokens/cookies from local files and keychain
- Reads local data files (SQLite DBs, JSON logs)
- Sends tokens and data to Runway server via /api/ingest
- Server uses tokens to make API calls
- Supports daemon mode with offline queue and retry

IMPORTANT: This sidecar does NOT make API calls directly.
All API calls are done by the server using tokens we provide.
"""

import os
import sys
import json
import argparse
import datetime
import subprocess
import sqlite3
import socket
import hmac
import hashlib
import time
import platform
import shutil
import tempfile
import logging
import signal
import atexit
from pathlib import Path
from urllib import request, error
from typing import Dict, List, Optional, Any, Tuple

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
_pid_file_path: Optional[Path] = None
_hostname: Optional[str] = None  # Cached hostname from gethostname()
_windows_cred_cache: dict = {}  # cache {target: (password, ttl_timestamp)}
_windows_cred_ttl_seconds: int = 300  # Cache credential for 5 minutes


def get_sidecar_dir() -> Path:
    """Get the sidecar configuration directory."""
    if platform.system() == "Windows":
        app_data = os.getenv("APPDATA")
        if app_data:
            return Path(app_data) / "runway" / "sidecar"
        return Path.home() / "AppData" / "Roaming" / "runway" / "sidecar"
    else:
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


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
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
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    
    if file_enabled:
        ensure_dirs()
        log_path = get_log_path()
        file_handler = logging.FileHandler(log_path, mode='a')
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        handlers.append(file_handler)
    
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True
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
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal_handler)


# --- Queue Management ---

def queue_push(payload: Dict[str, Any]) -> None:
    """Add payload to offline queue."""
    ensure_dirs()
    queue_dir = get_queue_dir()
    
    # Create queue file for today
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    queue_file = queue_dir / f"{today}.jsonl"
    
    entry = {
        "ts": int(time.time()),
        "payload": payload
    }
    
    with open(queue_file, 'a') as f:
        f.write(json.dumps(entry, separators=(',', ':')) + '\n')
    
    logging.info(f"Queued payload for retry: {queue_file.name}")
    queue_rotate()


def queue_rotate(max_size_mb: int = 10, config: Optional[Dict[str, Any]] = None) -> None:
    """Rotate queue files, removing oldest if total size exceeds limit."""
    queue_dir = get_queue_dir()
    if not queue_dir.exists():
        return
    
    if max_size_mb is None and config:
        max_size_mb = config.get("queue_max_size_mb", 10)
    
    max_size_bytes = max_size_mb * 1024 * 1024
    
    # Get all queue files sorted by modification time (oldest first)
    queue_files = sorted(
        queue_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime
    )
    
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
    target_url = f"{api_url.rstrip('/')}/api/ingest"
    
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
                    
                    success, _ = http_post_signed_with_retry(
                        target_url, payload, api_key
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
                with open(queue_file, 'w') as f:
                    for line in failed_lines:
                        f.write(line + '\n')
                logging.warning(f"Queue file has {len(failed_lines)} failed entries: {queue_file.name}")
                
        except Exception as e:
            logging.error(f"Failed to process queue file {queue_file}: {e}")
    
    return count


# --- HTTP Utilities ---

def health_check(api_url: str, timeout: int = 5) -> bool:
    """Check if server is healthy before pushing."""
    try:
        req = request.Request(
            f"{api_url.rstrip('/')}/api/health",
            method="GET"
        )
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode() == 200
    except Exception:
        return False


def http_post_signed(url: str, data: Dict[str, Any], api_key: str) -> Tuple[bool, Any, int]:
    """POST data to URL with HMAC-SHA256 signature. Returns (success, data, code)."""
    timestamp = str(int(time.time()))
    body = json.dumps(data, separators=(',', ':')).encode("utf-8")
    
    signature = hmac.new(
        api_key.encode(),
        timestamp.encode() + body,
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Timestamp": timestamp
    }
    
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=15) as resp:
            return True, json.loads(resp.read().decode("utf-8")), resp.getcode()
    except error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode("utf-8")), e.code
        except:
            return False, e.reason, e.code
    except Exception as e:
        return False, str(e), 500


def http_post_signed_with_retry(
    url: str, 
    data: Dict[str, Any], 
    api_key: str,
    max_attempts: int = 3,
    backoff_seconds: int = 5
) -> Tuple[bool, Any, int]:
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
            wait = backoff_seconds * (2 ** attempt)
            logging.warning(f"Attempt {attempt + 1} failed, retrying in {wait}s...")
            time.sleep(wait)
    
    return False, last_error, last_code


def human_delta(target_dt):
    """Format datetime as human-readable delta."""
    if not target_dt:
        return "—"
    now = datetime.datetime.now(datetime.timezone.utc)
    if target_dt.tzinfo is None:
        target_dt = target_dt.replace(tzinfo=datetime.timezone.utc)
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
    elif system == "Darwin":
        return home / "Library/Application Support" / app_name
    else:
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
    elif system == "Darwin":
        return home / "Library/Application Support" / app_name
    else:
        xdg_config_home = os.getenv("XDG_CONFIG_HOME")
        if xdg_config_home:
            return Path(xdg_config_home) / app_name
        return home / ".config" / app_name


# --- Browser Cookie Extraction ---

def decrypt_chromium_cookie(encrypted_value, browser_name="Chrome"):
    """Decrypt a Chromium-based cookie value based on the current platform."""
    if not encrypted_value: return None
    system = platform.system()
    
    # macOS decryption
    if system == "Darwin":
        try:
            service = f"{browser_name} Safe Storage"
            if "Edge" in browser_name: service = "Microsoft Edge Safe Storage"
            
            cmd = ["security", "find-generic-password", "-s", service, "-w"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode != 0: return None
            
            password = result.stdout.strip()
            if encrypted_value.startswith(b'v10') or encrypted_value.startswith(b'v11'):
                import hashlib
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                salt = b'saltysalt'
                key = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), salt, 1003, 16)
                iv = b' ' * 16
                raw_ciphertext = encrypted_value[3:]
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                decryptor = cipher.decryptor()
                decrypted = decryptor.update(raw_ciphertext) + decryptor.finalize()
                pad_len = decrypted[-1]
                if 1 <= pad_len <= 16: return decrypted[:-pad_len].decode('utf-8')
        except: pass
        return None

    # Windows decryption
    elif system == "Windows":
        try:
            import ctypes
            from ctypes import wintypes
            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(wintypes.BYTE))]
            crypt32 = ctypes.windll.crypt32
            blob_in = DATA_BLOB()
            blob_in.cbData = len(encrypted_value)
            blob_in.pbData = ctypes.cast(encrypted_value, ctypes.POINTER(wintypes.BYTE))
            blob_out = DATA_BLOB()
            if crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
                buffer = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return buffer.decode('utf-8')
        except: pass
        return None

    # Linux decryption
    else:
        try:
            try: return encrypted_value.decode('utf-8')
            except: pass
            import hashlib
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            # Try secretstorage for Chrome/Edge keys
            import secretstorage
            conn = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(conn)
            password = None
            for item in collection.get_all_items():
                if item.get_label() in ["Chrome Safe Storage", "Chromium Safe Storage", "Microsoft Edge Safe Storage"]:
                    password = item.get_secret()
                    break
            if password and (encrypted_value.startswith(b'v10') or encrypted_value.startswith(b'v11')):
                salt = b'saltysalt'
                key = hashlib.pbkdf2_hmac('sha1', password, salt, 1003, 16)
                iv = b' ' * 16
                raw_ciphertext = encrypted_value[3:]
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                decryptor = cipher.decryptor()
                decrypted = decryptor.update(raw_ciphertext) + decryptor.finalize()
                pad_len = decrypted[-1]
                if 1 <= pad_len <= 16: return decrypted[:-pad_len].decode('utf-8')
        except: pass
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
            {"name": "Chrome", "darwin": "Google/Chrome", "linux": [".config/google-chrome"], "win": "Google/Chrome/User Data"},
            {"name": "Chromium", "darwin": "Chromium", "linux": [".config/chromium"], "win": "Chromium/User Data"},
            {"name": "Edge", "darwin": "Microsoft Edge", "linux": [".config/microsoft-edge"], "win": "Microsoft/Edge/User Data"},
        ]
        for v in variants:
            dirs = []
            if system == "Darwin": dirs.append(home / "Library/Application Support" / v["darwin"])
            elif system == "Windows": 
                la = os.getenv("LOCALAPPDATA")
                dirs.append(Path(la) / v["win"] if la else home / "AppData/Local" / v["win"])
            else:
                for lp in v["linux"]: dirs.append(home / lp)
            
            for base in dirs:
                if not base.exists(): continue
                for profile in ["Default", "Profile 1", "Profile 2"]:
                    for rel in [profile + "/Network/Cookies", profile + "/Cookies"]:
                        p = base / rel
                        if p.exists(): results.append({"browser": v["name"], "type": "chromium", "path": p})
        
        # 2. Firefox
        ff_dirs = []
        if system == "Darwin": ff_dirs.append(home / "Library/Application Support/Firefox/Profiles")
        elif system == "Windows": ff_dirs.append(home / "AppData/Roaming/Mozilla/Firefox/Profiles")
        else: ff_dirs.append(home / ".mozilla/firefox")
        for base in ff_dirs:
            if not base.exists(): continue
            for p in base.glob("*.default*"):
                if (p / "cookies.sqlite").exists():
                    results.append({"browser": "Firefox", "type": "firefox", "path": p / "cookies.sqlite"})
        
        # 3. Safari
        if system == "Darwin" and (home / "Library/Cookies/Cookies.binarycookies").exists():
            results.append({"browser": "Safari", "type": "safari", "path": home / "Library/Cookies/Cookies.binarycookies"})
            
        return results

    @staticmethod
    def parse_safari(path):
        """Minimal safari binary cookie parser."""
        try:
            with open(path, 'rb') as f:
                if f.read(4) != b'cook': return []
                num_pages = struct.unpack('>I', f.read(4))[0]
                page_sizes = [struct.unpack('>I', f.read(4))[0] for _ in range(num_pages)]
                all_cookies = []
                for size in page_sizes:
                    data = f.read(size)
                    if len(data) < 12: continue
                    num_c = struct.unpack('<I', data[4:8])[0]
                    off = [struct.unpack('<I', data[8+(i*4):12+(i*4)])[0] for i in range(num_c)]
                    for o in off:
                        c = data[o:]
                        u_o, n_o = struct.unpack('<I', c[16:20])[0], struct.unpack('<I', c[20:24])[0]
                        v_o = struct.unpack('<I', c[28:32])[0]
                        def r_s(at):
                            e = c.find(b'\x00', at)
                            return c[at:e].decode('utf-8', errors='replace') if e != -1 else ""
                        all_cookies.append({"domain": r_s(u_o), "name": r_s(n_o), "value": r_s(v_o)})
                return all_cookies
        except: return []

    @staticmethod
    def get_cookie(domain, name):
        for target in BrowserCookieExtractor.get_all_paths():
            try:
                if target["type"] == "safari":
                    for c in BrowserCookieExtractor.parse_safari(target["path"]):
                        if domain in c["domain"] and c["name"] == name: return c["value"]
                else:
                    conn = sqlite3.connect(f"file:{str(target['path'])}?mode=ro&uri=1", uri=True)
                    cursor = conn.cursor()
                    if target["type"] == "chromium":
                        cursor.execute("SELECT encrypted_value FROM cookies WHERE host_key LIKE ? AND name = ?", (f"%{domain}%", name))
                        row = cursor.fetchone()
                        if row:
                            val = decrypt_chromium_cookie(row[0], target["browser"])
                            if val: conn.close(); return val
                    else: # Firefox
                        cursor.execute("SELECT value FROM moz_cookies WHERE host LIKE ? AND name = ?", (f"%{domain}%", name))
                        row = cursor.fetchone()
                        if row: val = row[0]; conn.close(); return val
                    conn.close()
            except: continue
        return None


def get_windows_credential(target: str) -> Optional[str]:
    """Extract credential from Windows Credential Manager with caching."""
    import time
    
    if platform.system() != "Windows":
        return None
    
    # Return cached credential if still valid
    now = time.time()
    for cached_target, (password, ttl) in _windows_cred_cache.items():
        if now < ttl and cached_target == target:
            return password
    
    try:
        # Try using PowerShell to access Credential Manager
        cmd = [
            "powershell",
            "-Command",
            f"(New-Object System.Net.NetworkCredential('', (Get-StoredCredential -Target '{target}').Password)).Password"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            password = result.stdout.strip()
            if password:
                _windows_cred_cache[target] = (password, time.time() + _windows_cred_ttl_seconds)
                return password
    except Exception:
        pass
    
    # Fallback: Try using cmdkey
    try:
        cmd = ["cmdkey", "/list"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        # This only lists credentials, doesn't retrieve them
        # Would need additional parsing if we want to support this path
    except Exception:
        pass
    
    return None


# --- Token Extractors ---

class AnthropicCollector:
    """Extract Claude OAuth tokens from local sources."""
    
    @staticmethod
    def get_keychain_credentials():
        """Extract credentials from macOS Keychain."""
        if sys.platform != "darwin":
            return None, None
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                keychain_data = result.stdout.strip()
                try:
                    data = json.loads(keychain_data)
                    oauth_data = data.get("claudeAiOauth", {})
                    return oauth_data.get("accessToken"), oauth_data.get("refreshToken")
                except json.JSONDecodeError:
                    if keychain_data.startswith("sk-"):
                        return keychain_data, None
        except:
            pass
        return None, None
    
    @staticmethod
    def collect():
        """Extract OAuth tokens, send to server for API call."""
        access_token = None
        refresh_token = None
        
        # Priority 1: Env var
        access_token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
        
        # Priority 2: Credentials file
        if not access_token:
            potential_paths = [
                Path.home() / ".claude" / ".credentials.json",
                get_platform_config_dir("claude") / ".credentials.json"
            ]
            for cred_path in potential_paths:
                if cred_path.exists():
                    try:
                        with open(cred_path) as f:
                            data = json.load(f)
                            oauth_data = data.get("claudeAiOauth", {})
                            access_token = oauth_data.get("accessToken")
                            refresh_token = oauth_data.get("refreshToken")
                            if access_token:
                                break
                    except:
                        pass
        
        # Priority 3: macOS Keychain
        if not access_token:
            access_token, refresh_token = AnthropicCollector.get_keychain_credentials()
        
        if not access_token:
            return []
        
        detail_parts = [f"oauth_token:{access_token}"]
        if refresh_token:
            detail_parts.append(f"refresh_token:{refresh_token}")
        detail_parts.append("[Sidecar]")
        
        metadata = {"oauth_token": access_token}
        if refresh_token:
            metadata["refresh_token"] = refresh_token
            
        return [{
            "service": "Claude Pro",
            "icon": "🟠",
            "remaining": "Token",
            "unit": "oauth",
            "reset": "—",
            "health": "good",
            "pace": "Token",
            "detail": "[Token Extracted] [Sidecar]",
            "data_source": "token_extracted",
            "metadata": metadata
        }]


class GitHubCollector:
    """Extract GitHub token from local sources."""
    
    @staticmethod
    def collect():
        """Extract GitHub token, send to server for API call."""
        token = os.getenv("GITHUB_TOKEN")
        
        # Priority 2: gh CLI config
        if not token:
            potential_paths = [
                Path.home() / ".config" / "gh" / "hosts.yml",
                get_platform_config_dir("gh") / "hosts.yml"
            ]
            for gh_path in potential_paths:
                if gh_path.exists():
                    try:
                        import yaml
                        with open(gh_path) as f:
                            data = yaml.safe_load(f)
                            if data and "github.com" in data:
                                token = data["github.com"].get("oauth_token")
                                if token:
                                    break
                    except ImportError:
                        break
                    except Exception:
                        pass
        
        # Priority 3: Windows Credential Manager
        if not token and platform.system() == "Windows":
            token = get_windows_credential("github.com")
        
        if not token:
            return []
        
        return [{
            "service": "GitHub API",
            "icon": "🐙",
            "remaining": "Token",
            "unit": "api_key",
            "reset": "—",
            "health": "good",
            "pace": "Token",
            "detail": "[Token Extracted] [Sidecar]",
            "data_source": "token_extracted",
            "metadata": {"api_key": token}
        }]


class GeminiCollector:
    """Extract Gemini OAuth credentials from local files."""
    
    @staticmethod
    def collect():
        """Extract OAuth credentials, send to server for API call."""
        potential_paths = [
            Path.home() / ".gemini" / "oauth_creds.json",
            get_platform_config_dir("gemini") / "oauth_creds.json"
        ]
        
        creds_path = None
        for p in potential_paths:
            if p.exists():
                creds_path = p
                break
        
        if not creds_path:
            return []
        
        try:
            with open(creds_path) as f:
                creds = json.load(f)
            
            token = creds.get("access_token")
            if not token:
                return []
            
            return [{
                "service": "Gemini API",
                "icon": "🔵",
                "remaining": "Token",
                "unit": "oauth",
                "reset": "—",
                "health": "good",
                "pace": "Token",
                "detail": "[Token Extracted] [Sidecar]",
                "data_source": "token_extracted",
                "metadata": {"oauth_token": token}
            }]
        except:
            return []


class ChatGPTCollector:
    """Extract ChatGPT OAuth token from local sources."""
    
    @staticmethod
    def collect():
        """Extract OAuth token, send to server for API call."""
        token = os.getenv("CHATGPT_OAUTH_TOKEN")
        
        if not token:
            potential_paths = [
                Path.home() / ".codex" / "auth.json",
                get_platform_config_dir("codex") / "auth.json"
            ]
            for auth_path in potential_paths:
                if auth_path.exists():
                    try:
                        with open(auth_path) as f:
                            data = json.load(f)
                            token = data.get("tokens", {}).get("access_token")
                            if token:
                                break
                    except:
                        pass
        
        if not token:
            return []
        
        return [{
            "service": "ChatGPT Codex",
            "icon": "💬",
            "remaining": "Token",
            "unit": "oauth",
            "reset": "—",
            "health": "good",
            "pace": "Token",
            "detail": "[Token Extracted] [Sidecar]",
            "data_source": "token_extracted",
            "metadata": {"oauth_token": token}
        }]


class KimiCollector:
    """Extract Kimi auth token from Chrome cookies."""
    
    @staticmethod
    def _get_cookie(cookie_name: str = "kimi-auth") -> Optional[str]:
        """Extract cookie across all browsers."""
        return BrowserCookieExtractor.get_cookie("kimi.com", cookie_name)
    
    @staticmethod
    def collect():
        """Extract Kimi cookie, send to server for API call."""
        token = os.getenv("KIMI_AUTH_TOKEN")
        
        if not token:
            token = KimiCollector._get_cookie()
        
        if not token:
            return []
        
        return [{
            "service": "Kimi API",
            "icon": "🌙",
            "remaining": "Token",
            "unit": "cookie",
            "reset": "—",
            "health": "good",
            "pace": "Token",
            "detail": "[Cookie Extracted] [Sidecar]",
            "data_source": "token_extracted",
            "metadata": {"cookie_kimi-auth": token}
        }]


class ZaiCollector:
    """Extract ZAI API key from local sources."""
    
    @staticmethod
    def collect():
        """Extract API key, send to server for API call."""
        key = os.getenv("ZAI_API_KEY")
        
        if not key or key.lower() == "zai":
            return []
        
        return [{
            "service": "zAI API",
            "icon": "🌐",
            "remaining": "Token",
            "unit": "api_key",
            "reset": "—",
            "health": "good",
            "pace": "Token",
            "detail": "[Token Extracted] [Sidecar]",
            "data_source": "token_extracted",
            "metadata": {"api_key": key}
        }]


class OpenCodeCollector:
    """Read OpenCode local database."""
    
    @staticmethod
    def get_opencode_session():
        """Extract opencode.ai session across all browsers."""
        return BrowserCookieExtractor.get_cookie("opencode.ai", "session")
    
    @staticmethod
    def collect():
        """Read local OpenCode DB and extract session cookie."""
        results = []
        hostname = socket.gethostname()
        
        # 1. Session cookie for server Web API
        session = OpenCodeCollector.get_opencode_session()
        if session:
            results.append({
                "service": "OpenCode (Web Token)",
                "icon": "⚡",
                "remaining": "Cookie",
                "unit": "web",
                "reset": "—",
                "health": "good",
                "pace": "Token",
                "detail": "[Cookie Extracted] [Sidecar]",
                "data_source": "token_extracted",
                "metadata": {"cookie_session": session}
            })
        
        # 2. Local DB data
        potential_db_paths = [
            Path.home() / ".local" / "share" / "opencode" / "opencode.db",
            get_platform_data_dir("opencode") / "opencode.db"
        ]
        
        db_path = None
        for p in potential_db_paths:
            if p.exists():
                db_path = p
                break
        
        if db_path:
            try:
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                
                now = datetime.datetime.now(datetime.timezone.utc)
                
                cutoffs = {
                    "5h": int((now - datetime.timedelta(hours=5)).timestamp() * 1000),
                    "week": int((now - datetime.timedelta(days=7)).timestamp() * 1000),
                    "month": int((now - datetime.timedelta(days=30)).timestamp() * 1000),
                }
                
                limits = {"5h": 12.0, "week": 30.0, "month": 60.0}
                
                for window, cutoff_ms in cutoffs.items():
                    cursor.execute("""
                        SELECT SUM(json_extract(data, '$.cost')), COUNT(*)
                        FROM message
                        WHERE time_created > ?
                          AND json_valid(data)
                          AND json_extract(data, '$.role') = 'assistant'
                    """, (cutoff_ms,))
                    
                    row = cursor.fetchone()
                    used = float(row[0] or 0.0)
                    count = int(row[1] or 0)
                    limit = limits[window]
                    remaining = max(0, limit - used)
                    pct = (used / limit * 100) if limit > 0 else 0
                    
                    window_labels = {"5h": "5 Hours", "week": "7 Days", "month": "30 Days"}
                    
                    results.append({
                        "service": f"OpenCode ({window_labels[window]})",
                        "icon": "⚡",
                        "remaining": f"${remaining:.2f}",
                        "unit": f"${limit:.0f} limit",
                        "reset": f"Rolling {window}",
                        "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                        "pace": "Stable" if pct < 50 else "High" if pct < 80 else "Fatigue",
                        "detail": f"${used:.2f} used · {count} msgs · {hostname} [Sidecar]",
                        "data_source": "local",
                        "metadata": {
                            "used": used,
                            "count": count,
                            "window": window,
                            "hostname": hostname
                        }
                    })
                
                conn.close()
            except:
                pass
        
        return results


class AntigravityCollector:
    """Read Antigravity local quota file."""
    
    @staticmethod
    def collect():
        """Read local Antigravity quota file."""
        potential_paths = [
            Path.home() / ".antigravity" / "state" / "quota.json",
            get_platform_data_dir("antigravity") / "state" / "quota.json"
        ]
        
        path = None
        for p in potential_paths:
            if p.exists():
                path = p
                break
        
        if not path:
            return []
        
        try:
            with open(path) as f:
                data = json.load(f)
            
            results = []
            for name, usage in data.get("models", {}).items():
                rem = usage.get("remaining_percent", 0.0)
                reset_ts = usage.get("resets_at")
                reset_at = datetime.datetime.fromtimestamp(reset_ts, tz=datetime.timezone.utc) if reset_ts else None
                
                results.append({
                    "service": f"AG: {name}",
                    "icon": "🛸",
                    "remaining": f"{rem:.1f}%",
                    "unit": "remaining",
                    "reset": human_delta(reset_at),
                    "health": "good" if rem > 30 else "warning",
                    "pace": "Stable",
                    "detail": f"{name} [Sidecar]",
                    "data_source": "local",
                    "metadata": {
                        "name": name,
                        "remaining_percent": rem,
                        "resets_at": reset_ts
                    }
                })
            
            return results
        except:
            return []


# --- Collection Runner ---

def collect_metrics(provider: str) -> List[Dict[str, Any]]:
    """Collect metrics from specified provider(s)."""
    providers_map = {
        "anthropic": AnthropicCollector,
        "github": GitHubCollector,
        "gemini": GeminiCollector,
        "chatgpt": ChatGPTCollector,
        "kimi": KimiCollector,
        "zai": ZaiCollector,
        "opencode": OpenCodeCollector,
        "antigravity": AntigravityCollector,
    }
    
    all_metrics = []
    
    if provider == "all":
        providers_list = list(providers_map.values())
    else:
        if provider not in providers_map:
            logging.error(f"Unknown provider: {provider}")
            return []
        providers_list = [providers_map[provider]]
    
    for p in providers_list:
        try:
            metrics = p.collect()
            all_metrics.extend(metrics)
            logging.debug(f"Collected {len(metrics)} metrics from {p.__name__}")
        except Exception as e:
            logging.error(f"Error collecting from {p.__name__}: {e}")
    
    return all_metrics


# --- Daemon Mode ---

def run_daemon(config: Dict[str, Any]) -> None:
    """Run sidecar in daemon mode with periodic collection."""
    global _daemon_running
    
    # Setup PID file
    if not write_pid_file():
        sys.exit(1)
    
    atexit.register(cleanup)
    setup_signal_handlers()
    
    _daemon_running = True
    interval = config.get("interval_seconds", 1800)
    api_url = config["api_url"]
    api_key = config["api_key"]
    providers = config.get("providers", ["all"])
    # Clear credential cache when starting daemon
    _windows_cred_cache = {}
    
    logging.info(f"Daemon started (PID: {os.getpid()}), interval: {interval}s")
    logging.info(f"API URL: {api_url}")
    logging.info(f"Providers: {providers}")
    
    while _daemon_running:
        start_time = time.time()
        
        try:
            # Flush any queued payloads first
            flushed = queue_flush(api_url, api_key)
            if flushed > 0:
                logging.info(f"Flushed {flushed} queued payloads")
            
            # Check server health  
            if not health_check(api_url):
                logging.warning("Server unreachable, queuing metrics for retry")
                # Still collect and queue
                for provider in providers:
                    metrics = collect_metrics(provider)
                    if metrics:
                        payload = {
                            "provider": f"{provider}-{get_hostname()}",
                            "metrics": metrics
                        }
                        queue_push(payload)
            else:
                # Collect and push metrics
                for provider in providers:
                    metrics = collect_metrics(provider)
                    if not metrics:
                        continue
                    
                    payload = {
                        "provider": f"{provider}-{get_hostname()}",
                        "metrics": metrics
                    }
                    
                    target_url = f"{api_url.rstrip('/')}/api/ingest"
                    success, data, code = http_post_signed_with_retry(
                        target_url, payload, api_key,
                        max_attempts=config.get("retry_attempts", 3),
                        backoff_seconds=config.get("retry_backoff_seconds", 5)
                    )
                    
                    if success:
                        logging.info(f"Pushed {len(metrics)} metrics for {provider}")
                    else:
                        logging.error(f"Failed to push {provider}: HTTP {code}")
                        queue_push(payload)
        
        except Exception as e:
            logging.error(f"Error in daemon loop: {e}")
        
        # Sleep until next interval
        elapsed = time.time() - start_time
        sleep_time = max(0, interval - elapsed)
        
        if sleep_time > 0 and _daemon_running:
            logging.debug(f"Sleeping for {sleep_time:.0f}s")
            time.sleep(sleep_time)


# --- Legacy Installer ---

def run_install(api_url, api_key):
    """Install sidecar as scheduled task (legacy mode)."""
    print("\n--- Sidecar Installer ---")
    print("Note: Consider using --daemon mode instead for real-time updates")
    
    if not api_url:
        api_url = input("Enter Runway API URL (e.g. http://localhost:8765): ").strip()
    if not api_key:
        api_key = input("Enter Ingestion API Key: ").strip()
    
    script_path = os.path.abspath(__file__)
    
    if sys.platform == "win32":
        cmd = f'schtasks /create /tn "RunwaySidecar" /tr "python {script_path} --api-url {api_url} --api-key {api_key}" /sc minute /mo 30 /f'
        try:
            subprocess.run(cmd, shell=True, check=True)
            print("SUCCESS: Task Scheduler entry created (Every 30m).")
        except Exception as e:
            print(f"ERROR: Failed to create Task Scheduler entry: {e}")
    else:
        cron_entry = f"*/30 * * * * {sys.executable} {script_path} --api-url {api_url} --api-key {api_key} > /dev/null 2>&1\n"
        try:
            current_cron = subprocess.check_output("crontab -l", shell=True, stderr=subprocess.STDOUT).decode("utf-8")
        except:
            current_cron = ""
        
        if script_path in current_cron:
            print("INFO: Task already exists in crontab. Updating...")
            lines = [l for l in current_cron.splitlines() if script_path not in l]
            current_cron = "\n".join(lines) + "\n"
        
        with subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE) as proc:
            proc.communicate(input=(current_cron + cron_entry).encode("utf-8"))
        
        print("SUCCESS: Crontab entry created (Every 30m).")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Runway Sidecar - Token & Data Collector")
    parser.add_argument("--provider", default="all", help="Provider to collect (default: all)")
    parser.add_argument("--api-url", help="Runway API URL (legacy, use --config)")
    parser.add_argument("--api-key", help="Ingestion API Key (legacy, use --config)")
    parser.add_argument("--config", "-c", help="Path to config file (default: ~/.config/runway/sidecar/config.json)")
    parser.add_argument("--install", action="store_true", help="Install as scheduled task (legacy)")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in daemon mode with periodic collection")
    parser.add_argument("--dry-run", action="store_true", help="Print without pushing")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    
    args = parser.parse_args()
    
    # Setup basic logging early
    log_level = "DEBUG" if args.verbose else "INFO"
    setup_logging(log_level, file_enabled=False)
    
    # Legacy install mode
    if args.install:
        run_install(args.api_url, args.api_key)
        return
    
    # Load configuration
    try:
        config = load_config(args.config)
    except SystemExit:
        raise
    
    # Override with CLI args if provided (legacy support)
    if args.api_url:
        config["api_url"] = args.api_url
    if args.api_key:
        config["api_key"] = args.api_key
    
    # Re-setup logging with config
    setup_logging(
        config.get("log_level", "INFO"),
        file_enabled=config.get("log_file_enabled", True)
    )
    
    # Daemon mode
    if args.daemon:
        run_daemon(config)
        return
    
    # One-shot mode
    api_key = config["api_key"]
    api_url = config["api_url"]
    provider = args.provider
    
    # Collect metrics
    metrics = collect_metrics(provider)
    
    if not metrics:
        logging.warning("No metrics collected.")
        return
    
    if args.dry_run:
        logging.info(f"Dry Run: {len(metrics)} metrics collected.")
        print(json.dumps(metrics, indent=2))
        return
    
    # Determine provider name
    hostname = socket.gethostname()
    if provider == "all":
        provider_name = f"sidecar-{hostname}"
    else:
        provider_name = f"{provider}-{hostname}"
    
    # Build payload
    payload = {
        "provider": provider_name,
        "metrics": metrics
    }
    
    # Flush any queued payloads first
    flushed = queue_flush(api_url, api_key)
    if flushed > 0:
        logging.info(f"Flushed {flushed} queued payloads")
    
    # Check server health
    if not health_check(api_url):
        logging.warning("Server unreachable, queuing metrics for retry")
        queue_push(payload)
        return
    
    # Push to server
    target_url = f"{api_url.rstrip('/')}/api/ingest"
    success, data, code = http_post_signed_with_retry(
        target_url, payload, api_key,
        max_attempts=config.get("retry_attempts", 3),
        backoff_seconds=config.get("retry_backoff_seconds", 5)
    )
    
    if success:
        logging.info(f"Pushed {len(metrics)} metrics to {target_url}")
        if isinstance(data, dict):
            logging.info(f"  Tokens: {data.get('tokens_received', 0)}, Metrics: {data.get('metrics_stored', 0)}")
    else:
        logging.error(f"HTTP {code}: {data}")
        queue_push(payload)


if __name__ == "__main__":
    main()
