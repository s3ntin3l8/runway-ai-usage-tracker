"""
Cross-platform Chrome cookie decryption for opencode.ai session extraction.

Supports macOS (Keychain), Windows (DPAPI), and Linux (Secret Service/unencrypted).
"""
import os
import sys
import sqlite3
import platform
import shutil
import tempfile
from pathlib import Path
from typing import Optional, List


def get_all_chrome_cookies_paths() -> List[Path]:
    """Get all potential paths to Chrome's Cookies databases across different profiles."""
    system = platform.system()
    home = Path.home()
    paths = []
    
    # Base user data directories
    base_dirs = []
    if system == "Darwin":
        base_dirs.append(home / "Library/Application Support/Google/Chrome")
    elif system == "Windows":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            base_dirs.append(Path(local_app_data) / "Google/Chrome/User Data")
        else:
            base_dirs.append(home / "AppData/Local/Google/Chrome/User Data")
    else:  # Linux
        # Standard
        base_dirs.append(home / ".config/google-chrome")
        base_dirs.append(home / ".config/chromium")
        # Snap
        base_dirs.append(home / "snap/google-chrome/common/.config/google-chrome")
        base_dirs.append(home / "snap/chromium/common/.config/chromium")
        # Flatpak
        base_dirs.append(home / ".var/app/com.google.Chrome/config/google-chrome")
    
    # Profiles to search
    profiles = ["Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4", "Profile 5"]
    
    for base in base_dirs:
        if not base.exists():
            continue
            
        for profile in profiles:
            # Different relative paths across Chrome versions
            potential_rel_paths = [
                profile + "/Network/Cookies",
                profile + "/Cookies",
            ]
            for rel_path in potential_rel_paths:
                p = base / rel_path
                if p.exists():
                    paths.append(p)
    
    return paths


def get_chrome_cookies_path() -> Optional[Path]:
    """Get the first existing path to Chrome's Cookies database."""
    paths = get_all_chrome_cookies_paths()
    return paths[0] if paths else None


def decrypt_macos_cookie(encrypted_value: bytes) -> Optional[str]:
    """Decrypt a cookie value using macOS Keychain."""
    try:
        import subprocess
        
        # Chrome on macOS uses AES-128-CBC with a key derived from the
        # 'Chrome Safe Storage' password stored in Keychain (PBKDF2-HMAC-SHA1,
        # salt=b'saltysalt', 1003 iterations, 16-byte key, fixed 16-space IV).
        # Retrieve the key from Keychain via the 'security' command.
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Chrome Safe Storage", "-w"],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            return None
        
        password = result.stdout.strip()
        
        # Chrome v10/v11 cookies use AES-128-CBC with a fixed 16-space IV.
        # The 3-byte prefix (v10/v11) is followed directly by the CBC ciphertext.
        if encrypted_value.startswith(b'v10') or encrypted_value.startswith(b'v11'):
            import hashlib
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            salt = b'saltysalt'
            key = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), salt, 1003, 16)
            iv = b' ' * 16
            raw_ciphertext = encrypted_value[3:]  # strip v10/v11 prefix

            try:
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                decryptor = cipher.decryptor()
                decrypted = decryptor.update(raw_ciphertext) + decryptor.finalize()
                # Strip PKCS7 padding
                pad_len = decrypted[-1]
                if pad_len < 1 or pad_len > 16:
                    return None
                return decrypted[:-pad_len].decode('utf-8')
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
                    
                    # Chrome v10/v11 cookies use AES-128-CBC with a fixed 16-space IV.
                    if encrypted_value.startswith(b'v10') or encrypted_value.startswith(b'v11'):
                        import hashlib
                        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

                        salt = b'saltysalt'
                        key = hashlib.pbkdf2_hmac('sha1', password, salt, 1003, 16)
                        iv = b' ' * 16
                        raw_ciphertext = encrypted_value[3:]  # strip v10/v11 prefix

                        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                        decryptor = cipher.decryptor()
                        decrypted = decryptor.update(raw_ciphertext) + decryptor.finalize()
                        # Strip PKCS7 padding
                        pad_len = decrypted[-1]
                        if pad_len < 1 or pad_len > 16:
                            return None
                        return decrypted[:-pad_len].decode('utf-8')
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
    Searches across all available Chrome profiles.
    
    Returns:
        The decrypted session cookie value, or None if not found/decryption failed.
    """
    cookies_paths = get_all_chrome_cookies_paths()
    if not cookies_paths:
        return None
    
    for cookies_path in cookies_paths:
        temp_path = None
        try:
            # Create a temporary file copy to avoid "database is locked" errors
            fd, temp_path = tempfile.mkstemp()
            os.close(fd)
            shutil.copy2(str(cookies_path), temp_path)
            
            # Connect to the temporary copy
            conn = sqlite3.connect(temp_path)
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
        except Exception:
            continue
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            
    return None


def get_claude_session_cookie() -> Optional[str]:
    """
    Extract the sessionKey cookie for claude.ai from Chrome's cookie store.
    Searches across all available Chrome profiles.
    
    Returns:
        The decrypted sessionKey cookie value (e.g., 'sk-ant-...'), or None
        if not found or decryption failed.
    """
    cookies_paths = get_all_chrome_cookies_paths()
    if not cookies_paths:
        return None
    
    for cookies_path in cookies_paths:
        temp_path = None
        try:
            # Create a temporary file copy to avoid "database is locked" errors
            fd, temp_path = tempfile.mkstemp()
            os.close(fd)
            shutil.copy2(str(cookies_path), temp_path)
            
            # Connect to the temporary copy
            conn = sqlite3.connect(temp_path)
            cursor = conn.cursor()
            
            # Query for claude.ai sessionKey cookie
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
        except Exception:
            continue
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            
    return None


def get_kimi_auth_cookie() -> Optional[str]:
    """
    Extract the kimi-auth cookie for kimi.com from Chrome's cookie store.
    Searches across all available Chrome profiles.
    
    Returns:
        The decrypted kimi-auth cookie value (JWT token), or None
        if not found or decryption failed.
    """
    cookies_paths = get_all_chrome_cookies_paths()
    if not cookies_paths:
        return None
    
    for cookies_path in cookies_paths:
        temp_path = None
        try:
            # Create a temporary file copy to avoid "database is locked" errors
            fd, temp_path = tempfile.mkstemp()
            os.close(fd)
            shutil.copy2(str(cookies_path), temp_path)
            
            # Connect to the temporary copy
            conn = sqlite3.connect(temp_path)
            cursor = conn.cursor()
            
            # Query for kimi.com kimi-auth cookie
            # Try multiple possible cookie names
            for cookie_name in ['kimi-auth', 'kimi_token', 'auth_token']:
                cursor.execute(
                    "SELECT encrypted_value FROM cookies WHERE host_key LIKE '%kimi.com%' AND name = ?",
                    (cookie_name,)
                )
                
                row = cursor.fetchone()
                if row:
                    encrypted_value = row[0]
                    
                    # Try to decrypt
                    decrypted = decrypt_cookie(encrypted_value)
                    if decrypted:
                        conn.close()
                        return decrypted
                    
                    # If decryption failed, try treating as plaintext (some configs)
                    try:
                        plaintext = encrypted_value.decode('utf-8')
                        conn.close()
                        return plaintext
                    except UnicodeDecodeError:
                        pass
            
            conn.close()
        except Exception:
            continue
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            
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
