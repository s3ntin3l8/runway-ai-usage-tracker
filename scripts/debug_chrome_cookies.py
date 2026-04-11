#!/usr/bin/env python3
"""
Comprehensive debug script for Chrome/Chromium cookie extraction and decryption.
Supports macOS, Windows, and Linux.

This script attempts to locate the Chrome cookie database, query for cookies,
and test the OS-specific decryption mechanisms (Keychain, DPAPI, or Secret Service).
"""

import os
import sys
import sqlite3
import platform
import shutil
import tempfile
import logging
from pathlib import Path

# Setup logging to output directly to console
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Add project root to Python path so we can import app modules
project_root = str(Path(__file__).parent.parent.absolute())
if project_root not in sys.path:
    sys.path.insert(0, project_root)

try:
    from app.core.browser_cookies import (
        get_all_browser_cookies_paths,
        decrypt_chromium_cookie,
    )
except ImportError as e:
    logger.error(f"Failed to import app modules: {e}")
    logger.error(f"Python path: {sys.path}")
    sys.exit(1)


def debug_chrome_extraction():
    logger.info("=== Chrome/Chromium Cookie Extraction Debugger ===")
    
    sys_os = platform.system()
    logger.info(f"Operating System: {sys_os}")
    
    # 1. Find Chrome/Chromium paths
    all_targets = get_all_browser_cookies_paths()
    chrome_targets = [t for t in all_targets if t.get("type") == "chromium"]
    
    if not chrome_targets:
        logger.error("No Chromium-based browser profiles found in standard locations.")
        return

    logger.info(f"Found {len(chrome_targets)} potential Chromium cookie databases.")
    
    success_count = 0
    
    for target in chrome_targets:
        browser_name = target["browser"]
        db_path = target["path"]
        
        logger.info("-" * 50)
        logger.info(f"Testing Browser: {browser_name}")
        logger.info(f"Database Path: {db_path}")
        
        if not os.path.exists(db_path):
            logger.warning(f"Path does not exist: {db_path}")
            continue
            
        logger.info("Database file exists. Attempting connection...")
        
        temp_path = None
        try:
            # Copy to temp file to bypass file locks
            fd, temp_path = tempfile.mkstemp(suffix=".sqlite")
            os.close(fd)
            shutil.copy2(str(db_path), temp_path)
            
            conn = sqlite3.connect(temp_path)
            cursor = conn.cursor()
            
            # Check schema
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cookies'")
            if not cursor.fetchone():
                logger.error("Table 'cookies' not found in database. Is this a valid Chrome cookie DB?")
                conn.close()
                continue
                
            # Fetch a few encrypted cookies to test decryption
            cursor.execute("SELECT host_key, name, encrypted_value FROM cookies WHERE encrypted_value IS NOT NULL LIMIT 5")
            rows = cursor.fetchall()
            
            if not rows:
                logger.warning("No encrypted cookies found in this database.")
                conn.close()
                continue
                
            logger.info(f"Found {len(rows)} encrypted cookies for testing.")
            
            decryption_success = False
            for host_key, name, encrypted_value in rows:
                logger.debug(f"Attempting to decrypt cookie '{name}' for domain '{host_key}'...")
                
                decrypted = decrypt_chromium_cookie(encrypted_value)
                
                if decrypted:
                    logger.info(f"✅ SUCCESS: Successfully decrypted cookie '{name}' for '{host_key}'")
                    decryption_success = True
                    break # One success proves the mechanism works
                else:
                    logger.debug(f"Failed to decrypt cookie '{name}' for '{host_key}'")
                    
            if decryption_success:
                success_count += 1
            else:
                logger.error("❌ FAILED: Could not decrypt any of the test cookies.")
                if sys_os == "Darwin":
                    logger.error("  -> On macOS, this usually means Python lacks Keychain permissions ('Chrome Safe Storage').")
                    logger.error("  -> Try running `security find-generic-password -s 'Chrome Safe Storage' -g` in this terminal.")
                elif sys_os == "Windows":
                    logger.error("  -> On Windows, DPAPI decryption failed. Are you running as the user who created the cookies?")
                elif sys_os == "Linux":
                    logger.error("  -> On Linux, Secret Service/KWallet integration might be failing.")
                    
            conn.close()
            
        except sqlite3.OperationalError as e:
            logger.error(f"SQLite Error (Database might be locked or corrupted): {e}")
        except Exception as e:
            logger.error(f"Unexpected error testing {browser_name}: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception as e:
                    logger.debug(f"Failed to cleanup temp file {temp_path}: {e}")

    logger.info("=" * 50)
    logger.info(f"Debug Complete. Successfully verified decryption for {success_count}/{len(chrome_targets)} Chrome/Chromium profiles.")

if __name__ == "__main__":
    debug_chrome_extraction()
