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
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from app.core.config import settings
from app.core.registry import registry
from app.core.keychain import get_keychain_secret

logger = logging.getLogger(__name__)

# --- Chromium-based (Chrome, Edge) decryption ---


def decrypt_macos_cookie(encrypted_value: bytes, db_path: Optional[Path] = None) -> Optional[str]:
    """Decrypt a cookie value using macOS Keychain."""
    try:
        # Use centralized keychain access with caching
        password = get_keychain_secret("Chrome Safe Storage", force_refresh=True)
        if not password:
            # Try Edge-specific storage if Chrome fails
            password = get_keychain_secret("Microsoft Edge Safe Storage", force_refresh=True)

        if not password:
            return None
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


def _get_windows_master_key(db_path: Path) -> Optional[bytes]:
    """Extract and decrypt the master key for Windows Chrome/Edge."""
    try:
        import json
        import base64
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(wintypes.BYTE)),
            ]

        # Locate Local State file by navigating up from db_path to User Data
        local_state_path = None
        current = db_path.parent
        for _ in range(4):
            test_path = current / "Local State"
            if test_path.exists():
                local_state_path = test_path
                break
            current = current.parent
            if current == current.parent:
                break
        
        if not local_state_path:
            return None

        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        
        encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
        # Prefix "DPAPI" is 5 bytes
        encrypted_key = encrypted_key[5:]

        crypt32 = ctypes.windll.crypt32
        blob_in = DATA_BLOB()
        blob_in.cbData = len(encrypted_key)
        blob_in.pbData = ctypes.cast(encrypted_key, ctypes.POINTER(wintypes.BYTE))
        blob_out = DATA_BLOB()

        if crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            master_key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return master_key
        return None
    except Exception as e:
        logger.debug(f"Error extracting Windows master key: {e}")
        return None


def decrypt_windows_cookie(encrypted_value: bytes, db_path: Optional[Path] = None) -> Optional[str]:
    """Decrypt a cookie value using Windows DPAPI or AES-GCM with Master Key."""
    if not encrypted_value:
        return None

    # Modern Chrome (v10/v20) uses AES-GCM
    if (encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11") or encrypted_value.startswith(b"v20")) and db_path:
        master_key = _get_windows_master_key(db_path)
        if not master_key:
            return None
        
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:]
            aesgcm = AESGCM(master_key)
            decrypted = aesgcm.decrypt(nonce, ciphertext, None)
            return decrypted.decode("utf-8")
        except Exception:
            return None

    # Legacy DPAPI
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


def decrypt_linux_cookie(encrypted_value: bytes, db_path: Optional[Path] = None) -> Optional[str]:
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


def decrypt_chromium_cookie(encrypted_value: bytes, db_path: Optional[Path] = None) -> Optional[str]:
    """Decrypt a Chromium-based cookie value based on the current platform."""
    system = platform.system()
    if system == "Darwin":
        return decrypt_macos_cookie(encrypted_value, db_path)
    elif system == "Windows":
        return decrypt_windows_cookie(encrypted_value, db_path)
    else:
        return decrypt_linux_cookie(encrypted_value, db_path)


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
            
            # DEBUG: Print all found cookies
            # Cleanup
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

    # 1. Safari (PRIORITY on Darwin as it is unencrypted)
    if system == "Darwin":
        safari_paths = [
            home / "Library/Cookies/Cookies.binarycookies",
            home / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
        ]
        for p in safari_paths:
            if p and p.exists():
                results.append({"browser": "Safari", "type": "safari", "path": p})

    # 2. Chrome / Chromium / Edge (Chromium-based)
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

    # 3. Firefox
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

    return results


def get_session_cookie(domain_substring: str, cookie_name: str) -> Optional[str]:
    """Search for a single session cookie (legacy wrapper)."""
    cookies = get_session_cookies(domain_substring, cookie_name)
    return cookies[0] if cookies else None


def get_session_cookies(domain_substring: str, cookie_name: str, allow_prefix: bool = True) -> List[str]:
    """
    Search for one or more session cookies (supporting chunked NextAuth tokens).

    Args:
        domain_substring: String to match host_key (e.g., 'claude.ai')
        cookie_name: Base name of the cookie.
        allow_prefix: If True, also matches cookies with .0, .1, etc. suffixes.

    Returns:
        List of decrypted/parsed cookie values.
    """
    if not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
        return []

    targets = get_all_browser_cookies_paths()
    if not targets:
        return []

    logger.info(f"🍪 Searching for {cookie_name} cookies in domain {domain_substring}...")
    for target in targets:
        path = target["path"]
        b_type = target["type"]
        browser_name = target["browser"]

        if not os.path.exists(path):
            continue

        # Safari (Binary)
        if b_type == "safari":
            try:
                cookies = SafariBinaryCookieParser.parse_file(path)
                found = []
                for c in cookies:
                    if domain_substring in c["domain"]:
                        if c["name"] == cookie_name or (allow_prefix and c["name"].startswith(f"{cookie_name}.")):
                            found.append(c)
                
                if found:
                    # Sort by name to handle .0, .1, .2 order
                    found.sort(key=lambda x: x["name"])
                    logger.info(f"✅ Found {len(found)} {cookie_name} cookies in Safari")
                    return [c["value"] for c in found]
            except Exception as e:
                logger.info(f"❌ Safari parsing error: {e}")
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
                query = "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE ?"
                params = [f"%{domain_substring}%"]
                
                if allow_prefix:
                    query += " AND (name = ? OR name LIKE ?)"
                    params.extend([cookie_name, f"{cookie_name}.%"])
                else:
                    query += " AND name = ?"
                    params.append(cookie_name)

                cursor.execute(query, params)
                rows = cursor.fetchall()
                if rows:
                    # Sort by name
                    rows.sort(key=lambda x: x[0])
                    results = []
                    for name, enc_val in rows:
                        decrypted = decrypt_chromium_cookie(enc_val, path)
                        if decrypted:
                            results.append(decrypted)
                    
                    if results:
                        logger.info(f"✅ Found {len(results)} {cookie_name} cookies in {browser_name}")
                        conn.close()
                        return results

            elif b_type == "firefox":
                query = "SELECT name, value FROM moz_cookies WHERE host LIKE ?"
                params = [f"%{domain_substring}%"]
                
                if allow_prefix:
                    query += " AND (name = ? OR name LIKE ?)"
                    params.extend([cookie_name, f"{cookie_name}.%"])
                else:
                    query += " AND name = ?"
                    params.append(cookie_name)

                cursor.execute(query, params)
                rows = cursor.fetchall()
                if rows:
                    rows.sort(key=lambda x: x[0])
                    logger.info(f"✅ Found {len(rows)} {cookie_name} cookies in Firefox")
                    vals = [r[1] for r in rows]
                    conn.close()
                    return vals

            conn.close()
        except Exception as e:
            logger.debug(f"Error checking {browser_name} database: {e}")
            continue
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    return []


# --- Registry-aware functions ---


def get_cookie_from_registry(provider_id: str, cookie_name: Optional[str] = None) -> Optional[str]:
    """Extract cookie for a provider using rules from registry.json."""
    provider_config = registry.get_provider(provider_id)
    rules = provider_config.get("rules", [])
    
    for rule in rules:
        if rule.get("type") == "cookie":
            if cookie_name and rule.get("name") != cookie_name:
                continue
            
            c_name = rule.get("name")
            for domain in rule.get("domains", []):
                val = get_session_cookie(domain, c_name)
                if val:
                    return val
    return None


# --- Legacy compatibility functions ---


def get_opencode_session_cookie() -> Optional[str]:
    return get_cookie_from_registry("opencode")


def get_claude_session_cookie() -> Optional[str]:
    return get_cookie_from_registry("anthropic")


def get_kimi_auth_cookie() -> Optional[str]:
    return get_cookie_from_registry("kimi", "kimi-auth")


def get_chatgpt_session_token() -> Optional[str]:
    """Extract ChatGPT session token from browser cookies."""
    return get_cookie_from_registry("chatgpt", "__Secure-next-auth.session-token")


def get_chatgpt_device_id() -> Optional[str]:
    """Extract ChatGPT device ID from browser cookies."""
    return get_session_cookie("chatgpt.com", "oai-device-id")


def get_macos_keychain_token(service: str, account: str) -> Optional[str]:
    """Fetch a token directly from macOS Keychain."""
    return get_keychain_secret(service, account)
