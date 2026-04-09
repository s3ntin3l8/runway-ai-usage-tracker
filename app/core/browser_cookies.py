"""
Cross-platform multi-browser cookie extraction for session data retrieval.
Supported Browsers: Chrome, Chromium, Microsoft Edge, Firefox, Safari (macOS).

Encryption Support:
- Chromium-based (Chrome, Edge): macOS (Keychain), Windows (DPAPI), Linux (Secret Service/unencrypted).
- Firefox: Unencrypted SQLite.
- Safari: Binary encoding (not encrypted).
"""

import os
import sys
import sqlite3
import platform
import shutil
import tempfile
import struct
import glob
from pathlib import Path
from typing import Optional, List, Dict, Any

# --- Chromium-based (Chrome, Edge) decryption ---


def decrypt_macos_cookie(encrypted_value: bytes) -> Optional[str]:
    """Decrypt a cookie value using macOS Keychain."""
    try:
        import subprocess

        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Chrome Safe Storage", "-w"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Try Edge-specific storage if Chrome fails
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    "Microsoft Edge Safe Storage",
                    "-w",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return None

        password = result.stdout.strip()
        if encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11"):
            import hashlib
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            salt = b"saltysalt"
            key = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), salt, 1003, 16)
            iv = b" " * 16
            raw_ciphertext = encrypted_value[3:]

            try:
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                decryptor = cipher.decryptor()
                decrypted = decryptor.update(raw_ciphertext) + decryptor.finalize()
                pad_len = decrypted[-1]
                if pad_len < 1 or pad_len > 16:
                    return None
                return decrypted[:-pad_len].decode("utf-8")
            except Exception:
                return None
        return None
    except Exception:
        return None


def decrypt_windows_cookie(encrypted_value: bytes) -> Optional[str]:
    """Decrypt a cookie value using Windows DPAPI."""
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
        return None
    except Exception:
        return None


def decrypt_linux_cookie(encrypted_value: bytes) -> Optional[str]:
    """Decrypt a cookie value on Linux (Chromium)."""
    try:
        try:
            return encrypted_value.decode("utf-8")
        except UnicodeDecodeError:
            pass

        try:
            import secretstorage

            connection = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(connection)

            # Search for Chrome or Edge keys
            password = None
            for item in collection.get_all_items():
                if item.get_label() in [
                    "Chrome Safe Storage",
                    "Chromium Safe Storage",
                    "Microsoft Edge Safe Storage",
                ]:
                    password = item.get_secret()
                    break

            if password and (
                encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11")
            ):
                import hashlib
                from cryptography.hazmat.primitives.ciphers import (
                    Cipher,
                    algorithms,
                    modes,
                )

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
        except ImportError:
            pass
        return None
    except Exception:
        return None


def decrypt_chromium_cookie(encrypted_value: bytes) -> Optional[str]:
    """Decrypt a Chromium-based cookie value based on the current platform."""
    system = platform.system()
    if system == "Darwin":
        return decrypt_macos_cookie(encrypted_value)
    elif system == "Windows":
        return decrypt_windows_cookie(encrypted_value)
    else:
        return decrypt_linux_cookie(encrypted_value)


# --- Safari Binary Cookies Parser ---


class SafariBinaryCookieParser:
    """Minimal parser for Safari's Cookies.binarycookies format."""

    @staticmethod
    def parse_file(file_path: Path) -> List[Dict[str, Any]]:
        cookies = []
        try:
            with open(file_path, "rb") as f:
                signature = f.read(4)
                if signature != b"cook":
                    return []

                num_pages = struct.unpack(">I", f.read(4))[0]
                page_sizes = [
                    struct.unpack(">I", f.read(4))[0] for _ in range(num_pages)
                ]

                for size in page_sizes:
                    page_data = f.read(size)
                    cookies.extend(SafariBinaryCookieParser.parse_page(page_data))
        except Exception:
            pass
        return cookies

    @staticmethod
    def parse_page(data: bytes) -> List[Dict[str, Any]]:
        page_cookies = []
        if len(data) < 12:
            return []
        num_cookies = struct.unpack("<I", data[4:8])[0]
        offsets = [
            struct.unpack("<I", data[8 + (i * 4) : 12 + (i * 4)])[0]
            for i in range(num_cookies)
        ]

        for offset in offsets:
            cookie_data = data[offset:]
            if len(cookie_data) < 48:
                continue

            # Offsets relative to the start of the cookie record
            url_offset = struct.unpack("<I", cookie_data[16:20])[0]
            name_offset = struct.unpack("<I", cookie_data[20:24])[0]
            path_offset = struct.unpack("<I", cookie_data[24:28])[0]
            value_offset = struct.unpack("<I", cookie_data[28:32])[0]

            def read_string(off):
                end = cookie_data.find(b"\x00", off)
                return (
                    cookie_data[off:end].decode("utf-8", errors="replace")
                    if end != -1
                    else ""
                )

            page_cookies.append(
                {
                    "domain": read_string(url_offset),
                    "name": read_string(name_offset),
                    "path": read_string(path_offset),
                    "value": read_string(value_offset),
                }
            )
        return page_cookies


# --- Path Discovery ---


def get_all_browser_cookies_paths() -> List[Dict[str, Any]]:
    """Get all potential paths to Cookies databases for all supported browsers."""
    system = platform.system()
    home = Path.home()
    results = []  # List of { 'browser': str, 'type': 'sqlite'|'binary', 'path': Path }

    # 1. Chrome / Chromium / Edge (Chromium-based)
    chromium_variants = [
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
            "linux": [
                ".config/microsoft-edge",
                ".config/microsoft-edge-beta",
                ".config/microsoft-edge-dev",
            ],
            "win": "Microsoft/Edge/User Data",
        },
    ]

    for variant in chromium_variants:
        base_dirs = []
        if system == "Darwin":
            base_dirs.append(home / "Library/Application Support" / variant["darwin"])
        elif system == "Windows":
            local_app_data = os.getenv("LOCALAPPDATA")
            base_dirs.append(
                Path(local_app_data) / variant["win"]
                if local_app_data
                else home / "AppData/Local" / variant["win"]
            )
        else:  # Linux
            for l_path in variant["linux"]:
                base_dirs.append(home / l_path)
                base_dirs.append(
                    home
                    / "snap"
                    / variant["name"].lower().replace(" ", "-")
                    / "common"
                    / l_path
                )

        profiles = [
            "Default",
            "Profile 1",
            "Profile 2",
            "Profile 3",
            "Profile 4",
            "Profile 5",
        ]
        for base in base_dirs:
            if not base.exists():
                continue
            for profile in profiles:
                for rel in [profile + "/Network/Cookies", profile + "/Cookies"]:
                    p = base / rel
                    if p.exists():
                        results.append(
                            {"browser": variant["name"], "type": "chromium", "path": p}
                        )

    # 2. Firefox
    ff_bases = []
    if system == "Darwin":
        ff_bases.append(home / "Library/Application Support/Firefox/Profiles")
    elif system == "Windows":
        ff_bases.append(home / "AppData/Roaming/Mozilla/Firefox/Profiles")
    else:  # Linux
        ff_bases.append(home / ".mozilla/firefox")
        ff_bases.append(home / "snap/firefox/common/.mozilla/firefox")

    for base in ff_bases:
        if not base.exists():
            continue
        # Firefox uses randomized profile names
        for p in base.glob("*.default*"):
            cookie_sqlite = p / "cookies.sqlite"
            if cookie_sqlite.exists():
                results.append(
                    {"browser": "Firefox", "type": "firefox", "path": cookie_sqlite}
                )

    # 3. Safari
    if system == "Darwin":
        safari_path = home / "Library/Cookies/Cookies.binarycookies"
        if safari_path.exists():
            results.append({"browser": "Safari", "type": "safari", "path": safari_path})

    return results


def get_session_cookie(domain_substring: str, cookie_name: str) -> Optional[str]:
    """
    Search for a session cookie across all supported browsers.

    Args:
        domain_substring: String to match host_key (e.g., 'claude.ai', 'opencode.ai')
        cookie_name: Precise name of the cookie (e.g., 'sessionKey', 'session')

    Returns:
        The decrypted/parsed cookie value, or None if not found.
    """
    if not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
        return None

    targets = get_all_browser_cookies_paths()
    if not targets:
        return None

    for target in targets:
        path = target["path"]
        b_type = target["type"]

        # Safari (Binary)
        if b_type == "safari":
            cookies = SafariBinaryCookieParser.parse_file(path)
            for c in cookies:
                if domain_substring in c["domain"] and c["name"] == cookie_name:
                    return c["value"]
            continue

        # SQLite browsers (Chromium and Firefox)
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp()
            os.close(fd)
            shutil.copy2(str(path), temp_path)

            conn = sqlite3.connect(temp_path)
            cursor = conn.cursor()

            if b_type == "chromium":
                cursor.execute(
                    "SELECT encrypted_value FROM cookies WHERE host_key LIKE ? AND name = ?",
                    (f"%{domain_substring}%", cookie_name),
                )
                row = cursor.fetchone()
                if row:
                    decrypted = decrypt_chromium_cookie(row[0])
                    if decrypted:
                        conn.close()
                        return decrypted

            elif b_type == "firefox":
                # Firefox schema: host, name, value
                cursor.execute(
                    "SELECT value FROM moz_cookies WHERE host LIKE ? AND name = ?",
                    (f"%{domain_substring}%", cookie_name),
                )
                row = cursor.fetchone()
                if row:
                    val = row[0]
                    conn.close()
                    return val

            conn.close()
        except Exception:
            continue
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass

    return None


# --- Legacy compatibility functions ---


def get_opencode_session_cookie() -> Optional[str]:
    return get_session_cookie("opencode.ai", "session")


def get_claude_session_cookie() -> Optional[str]:
    return get_session_cookie("claude.ai", "sessionKey")


def get_kimi_auth_cookie() -> Optional[str]:
    for name in ["kimi-auth", "kimi_token", "auth_token"]:
        val = get_session_cookie("kimi.com", name)
        if val:
            return val
    return None


def get_chatgpt_session_token() -> Optional[str]:
    """Extract ChatGPT session token from browser cookies."""
    return get_session_cookie("chatgpt.com", "__Secure-next-auth.session-token")


from app.core.config import settings


def get_macos_keychain_token(service: str, account: str) -> Optional[str]:
    """Still needed for direct keychain access in sidecar."""
    if not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
        return None

    if platform.system() != "Darwin":
        return None
    try:
        import subprocess

        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None
