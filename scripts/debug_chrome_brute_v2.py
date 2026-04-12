#!/usr/bin/env python3
import os
import sys
import sqlite3
import platform
import shutil
import tempfile
import logging
import json
import base64
import hashlib
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Add project root to Python path
project_root = str(Path(__file__).parent.parent.absolute())
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app.core.keychain import get_keychain_secret

def brute_force_decrypt(encrypted_value, password_raw):
    """Try all known variations of Chrome macOS decryption."""
    if not encrypted_value.startswith(b"v10") and not encrypted_value.startswith(b"v11"):
        return None
    
    ciphertext = encrypted_value[3:]
    
    # Variations to try
    passwords = [password_raw.encode("utf-8")]
    try:
        # If it looks like base64, try decoding it
        passwords.append(base64.b64decode(password_raw))
    except Exception:
        pass
        
    salts = [b"saltysalt", b"salt"] # Trying BOTH now
    iterations = [1003, 1000]
    ivs = [b" " * 16, b"\x00" * 16]
    
    for p in passwords:
        for salt in salts:
            for iters in iterations:
                for iv in ivs:
                    try:
                        key = hashlib.pbkdf2_hmac("sha1", p, salt, iters, 16)
                        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                        decryptor = cipher.decryptor()
                        decrypted = decryptor.update(ciphertext) + decryptor.finalize()
                        
                        # Check padding
                        pad_len = decrypted[-1]
                        if 1 <= pad_len <= 16:
                            try:
                                val = decrypted[:-pad_len].decode("utf-8")
                                # If we get a valid string, return it
                                return {
                                    "value": val,
                                    "params": f"pw_type={'b64' if p != passwords[0] else 'raw'}, salt={salt!r}, iters={iters}, iv={iv!r}"
                                }
                            except UnicodeDecodeError:
                                # Sometimes it might decrypt but not be UTF-8
                                pass
                    except Exception:
                        continue
    return None

def debug_brute_force():
    logger.info("=== Chrome macOS Decryption Brute-Force (v2: saltysalt vs salt) ===")
    
    password = get_keychain_secret("Chrome Safe Storage", force_refresh=True)
    if not password:
        logger.error("Could not get Keychain password.")
        return

    logger.info(f"Using Keychain Password: {password[:2]}...{password[-2:]} (Length: {len(password)})")
    
    db_path = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
    if not db_path.exists():
        logger.error(f"Database not found at {db_path}")
        return

    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        shutil.copy2(str(db_path), temp_path)
        
        conn = sqlite3.connect(temp_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name, host_key, encrypted_value FROM cookies WHERE encrypted_value IS NOT NULL LIMIT 20")
        rows = cursor.fetchall()
        
        success_count = 0
        for name, host, enc_val in rows:
            result = brute_force_decrypt(enc_val, password)
            if result:
                logger.info(f"✅ SUCCESS for cookie '{name}' on '{host}'!")
                logger.info(f"Working Params: {result['params']}")
                success_count += 1
                if success_count >= 1: # One success is enough to prove the params
                    break
        
        if success_count == 0:
            logger.error("❌ FAILED: Tried all combinations (including saltysalt), nothing worked.")
            logger.info("This confirms App-Bound encryption is active and blocking standard derivation.")
            
        conn.close()
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

if __name__ == "__main__":
    debug_brute_force()
