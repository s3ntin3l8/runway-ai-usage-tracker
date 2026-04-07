"""
Cross-platform Chrome cookie decryption for opencode.ai session extraction.

Supports macOS (Keychain), Windows (DPAPI), and Linux (Secret Service/unencrypted).
"""
import os
import sys
import sqlite3
import platform
from pathlib import Path
from typing import Optional


def get_chrome_cookies_path() -> Optional[Path]:
    """Get the path to Chrome's Cookies database based on platform."""
    system = platform.system()
    home = Path.home()
    
    if system == "Darwin":  # macOS
        path = home / "Library/Application Support/Google/Chrome/Default/Cookies"
    elif system == "Windows":
        path = home / "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies"
        if not path.exists():
            path = home / "AppData/Local/Google/Chrome/User Data/Default/Cookies"
    else:  # Linux
        path = home / ".config/google-chrome/Default/Cookies"
        if not path.exists():
            path = home / ".config/chromium/Default/Cookies"
    
    return path if path.exists() else None


def decrypt_macos_cookie(encrypted_value: bytes) -> Optional[str]:
    """Decrypt a cookie value using macOS Keychain."""
    try:
        import subprocess
        
        # Chrome on macOS uses AES-256-GCM with a key stored in Keychain
        # We need to use the 'security' command to get the key, or use ctypes
        # For now, try a simpler approach - check if cookies are unencrypted
        
        # Try to decrypt using Chrome's key from Keychain
        # This requires the 'Safe Storage' password
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Chrome Safe Storage", "-w"],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            return None
        
        password = result.stdout.strip()
        
        # The encrypted value has a 'v10' or 'v11' prefix followed by nonce + ciphertext + tag
        if encrypted_value.startswith(b'v10') or encrypted_value.startswith(b'v11'):
            # Extract nonce (12 bytes), ciphertext, and tag (16 bytes)
            prefix_len = 3
            nonce = encrypted_value[prefix_len:prefix_len + 12]
            ciphertext = encrypted_value[prefix_len + 12:-16]
            tag = encrypted_value[-16:]
            
            # Derive key using PBKDF2
            import hashlib
            import hmac
            
            salt = b'saltysalt'
            key = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), salt, 1003, 16)
            
            # Decrypt using AES-256-GCM
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            aesgcm = AESGCM(key)
            
            try:
                decrypted = aesgcm.decrypt(nonce, ciphertext + tag, None)
                return decrypted.decode('utf-8')
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
        
        # DPAPI decryption via CryptUnprotectData
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(wintypes.BYTE))
            ]
        
        # Load crypt32.dll
        crypt32 = ctypes.windll.crypt32
        
        # Prepare input data
        blob_in = DATA_BLOB()
        blob_in.cbData = len(encrypted_value)
        blob_in.pbData = ctypes.cast(encrypted_value, ctypes.POINTER(wintypes.BYTE))
        
        # Prepare output data
        blob_out = DATA_BLOB()
        
        # Call CryptUnprotectData
        if crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out)
        ):
            # Extract decrypted data
            buffer = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            # Free memory
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return buffer.decode('utf-8')
        
        return None
    except Exception:
        return None


def decrypt_linux_cookie(encrypted_value: bytes) -> Optional[str]:
    """Decrypt a cookie value on Linux."""
    try:
        # On Linux, Chrome may use GNOME Keyring or KWallet
        # Try to get the key from Secret Service
        
        # First, check if the cookie is actually unencrypted
        try:
            return encrypted_value.decode('utf-8')
        except UnicodeDecodeError:
            pass
        
        # Try using secretstorage
        try:
            import secretstorage
            
            connection = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(connection)
            
            # Look for Chrome Safe Storage
            for item in collection.get_all_items():
                if item.get_label() == "Chrome Safe Storage":
                    password = item.get_secret()
                    
                    # Decrypt using the password (similar to macOS)
                    if encrypted_value.startswith(b'v10') or encrypted_value.startswith(b'v11'):
                        import hashlib
                        
                        salt = b'saltysalt'
                        key = hashlib.pbkdf2_hmac('sha1', password, salt, 1003, 16)
                        
                        nonce = encrypted_value[3:3 + 12]
                        ciphertext = encrypted_value[3 + 12:-16]
                        tag = encrypted_value[-16:]
                        
                        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                        aesgcm = AESGCM(key)
                        
                        decrypted = aesgcm.decrypt(nonce, ciphertext + tag, None)
                        return decrypted.decode('utf-8')
        except ImportError:
            pass
        
        return None
    except Exception:
        return None


def decrypt_cookie(encrypted_value: bytes) -> Optional[str]:
    """Decrypt a Chrome cookie value based on the current platform."""
    system = platform.system()
    
    if system == "Darwin":
        return decrypt_macos_cookie(encrypted_value)
    elif system == "Windows":
        return decrypt_windows_cookie(encrypted_value)
    else:
        return decrypt_linux_cookie(encrypted_value)


def get_opencode_session_cookie() -> Optional[str]:
    """
    Extract the session cookie for opencode.ai from Chrome's cookie store.
    
    Returns:
        The decrypted session cookie value, or None if not found/decryption failed.
    """
    cookies_path = get_chrome_cookies_path()
    if not cookies_path:
        return None
    
    try:
        # Connect to Chrome's SQLite cookie database
        conn = sqlite3.connect(str(cookies_path))
        cursor = conn.cursor()
        
        # Query for opencode.ai session cookie
        cursor.execute(
            "SELECT encrypted_value FROM cookies WHERE host_key LIKE '%opencode.ai%' AND name = 'session'"
        )
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            encrypted_value = row[0]
            
            # Try to decrypt
            decrypted = decrypt_cookie(encrypted_value)
            if decrypted:
                return decrypted
            
            # If decryption failed, try treating as plaintext (some configs)
            try:
                return encrypted_value.decode('utf-8')
            except UnicodeDecodeError:
                pass
        
        return None
    except Exception:
        return None


def get_claude_session_cookie() -> Optional[str]:
    """
    Extract the sessionKey cookie for claude.ai from Chrome's cookie store.
    
    This cookie is used to authenticate with Claude's web API when OAuth
    credentials are not available. The cookie value starts with 'sk-ant-'.
    
    Returns:
        The decrypted sessionKey cookie value (e.g., 'sk-ant-...'), or None
        if not found or decryption failed.
    """
    cookies_path = get_chrome_cookies_path()
    if not cookies_path:
        return None
    
    try:
        # Connect to Chrome's SQLite cookie database
        conn = sqlite3.connect(str(cookies_path))
        cursor = conn.cursor()
        
        # Query for claude.ai sessionKey cookie
        # The cookie is scoped to claude.ai domain
        cursor.execute(
            "SELECT encrypted_value FROM cookies WHERE host_key LIKE '%claude.ai%' AND name = 'sessionKey'"
        )
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            encrypted_value = row[0]
            
            # Try to decrypt
            decrypted = decrypt_cookie(encrypted_value)
            if decrypted:
                return decrypted
            
            # If decryption failed, try treating as plaintext (some configs)
            try:
                return encrypted_value.decode('utf-8')
            except UnicodeDecodeError:
                pass
        
        return None
    except Exception:
        return None


def get_macos_keychain_token(service: str, account: str) -> Optional[str]:
    """
    Extract a token from macOS Keychain.
    
    Used by sidecar to get OAuth tokens when file-based credentials
    are not available. Queries the 'generic password' type.
    
    Args:
        service: The service name (e.g., "Claude Code-credentials")
        account: The account name (e.g., "credentials")
    
    Returns:
        The token/password value, or None if not found or not on macOS.
    """
    if platform.system() != "Darwin":
        return None
    
    try:
        import subprocess
        
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            return result.stdout.strip()
        
        return None
    except Exception:
        return None
