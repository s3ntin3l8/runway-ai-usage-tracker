#!/usr/bin/env python3
"""
Comprehensive debug script for Chrome/Chromium cookie extraction and decryption.
Supports macOS, Windows, and Linux.

This script attempts to locate all possible Chrome cookie databases, query for cookies,
and test the OS-specific decryption mechanisms.

Chrome 127+ on macOS/Windows enforces App-Bound Encryption (ABE), which ties the
decryption key to Chrome's own app bundle. This script will detect ABE and report
actionable workarounds if standard decryption fails.
"""

import json
import logging
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
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
        decrypt_chromium_cookie,
        get_all_browser_cookies_paths,
    )
    from app.core.keychain import get_keychain_secret
except ImportError as e:
    logger.error(f"Failed to import app modules: {e}")
    logger.error(f"Python path: {sys.path}")
    sys.exit(1)


def debug_chrome_extraction():
    logger.info("=== Chrome/Chromium Cookie Extraction Debugger ===")
    
    sys_os = platform.system()
    logger.info(f"Operating System: {sys_os}")
    
    # 1. Inspect Keychain Password (macOS specific)
    if sys_os == "Darwin":
        logger.info("-" * 50)
        logger.info("Checking macOS Keychain for 'Chrome Safe Storage'...")
        password = get_keychain_secret("Chrome Safe Storage", force_refresh=True)
        if password:
            logger.info(f"✅ Found password in Keychain (Length: {len(password)})")
            # Log first/last char safely for verification
            if len(password) > 2:
                logger.debug(f"Password starts with: {password[0]!r}, ends with: {password[-1]!r}")
        else:
            logger.error("❌ FAILED: 'Chrome Safe Storage' not found or access denied in Keychain.")

    # 2. Find ALL Chrome/Chromium paths (Globbing instead of hardcoded list)
    logger.info("-" * 50)
    logger.info("Discovering all available profiles...")
    
    # Custom glob-based discovery to "check everything"
    home = Path.home()
    all_targets = []
    
    if sys_os == "Darwin":
        base_dir = home / "Library/Application Support/Google/Chrome"
        if base_dir.exists():
            # Find all profile directories (Default, Profile X)
            for profile_dir in base_dir.glob("Default") : all_targets.append(("Chrome", profile_dir))
            for profile_dir in base_dir.glob("Profile *") : all_targets.append(("Chrome", profile_dir))
            
        edge_dir = home / "Library/Application Support/Microsoft Edge"
        if edge_dir.exists():
            for profile_dir in edge_dir.glob("Default") : all_targets.append(("Edge", profile_dir))
            for profile_dir in edge_dir.glob("Profile *") : all_targets.append(("Edge", profile_dir))
    
    # Fallback to standard discovery if glob found nothing
    if not all_targets:
        targets = get_all_browser_cookies_paths()
        for t in targets:
            if t.get("type") == "chromium":
                # Convert path to profile dir
                p = Path(t["path"])
                profile_dir = p.parent.parent if "Network" in str(p) else p.parent
                all_targets.append((t["browser"], profile_dir))

    if not all_targets:
        logger.error("No Chromium-based browser profiles found.")
        return

    logger.info(f"Found {len(all_targets)} potential Chromium profiles.")
    
    success_count = 0
    
    for browser_name, profile_dir in all_targets:
        # Check both modern and legacy cookie paths
        cookie_paths = [
            profile_dir / "Network/Cookies",
            profile_dir / "Cookies"
        ]
        
        db_path = None
        for p in cookie_paths:
            if p.exists():
                db_path = p
                break
        
        if not db_path:
            logger.debug(f"Skipping {profile_dir}: No Cookies file found.")
            continue
            
        logger.info("-" * 50)
        logger.info(f"Testing Profile: {profile_dir.name} ({browser_name})")
        logger.info(f"Database Path: {db_path}")
        
        # Check Local State for ABE status
        local_state_path = profile_dir.parent / "Local State"
        abe_suspected = False
        if local_state_path.exists():
            logger.info(f"Found 'Local State' file at {local_state_path}")
            try:
                with open(local_state_path) as f:
                    ls_data = json.load(f)
                    has_crypt = "os_crypt" in ls_data
                    logger.debug(f"Local State contains 'os_crypt' section: {has_crypt}")
                    if not has_crypt and sys_os == "Darwin":
                        logger.warning("⚠️  No 'os_crypt' in Local State — App-Bound Encryption (ABE) likely active")
                        abe_suspected = True
            except Exception:
                pass

        logger.info("Attempting connection to Cookies database...")
        temp_path = None
        try:
            # Copy to temp file to bypass file locks
            fd, temp_path = tempfile.mkstemp(suffix=".sqlite")
            os.close(fd)
            shutil.copy2(str(db_path), temp_path)
            
            conn = sqlite3.connect(temp_path)
            cursor = conn.cursor()
            
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cookies'")
            if not cursor.fetchone():
                logger.error("Table 'cookies' not found in database.")
                conn.close()
                continue
                
            cursor.execute("SELECT host_key, name, encrypted_value FROM cookies WHERE encrypted_value IS NOT NULL LIMIT 5")
            rows = cursor.fetchall()
            
            if not rows:
                logger.warning("No encrypted cookies found in this database.")
                conn.close()
                continue
                
            logger.info(f"Found {len(rows)} encrypted cookies for testing.")
            
            decryption_success = False
            abe_confirmed = False
            for host_key, name, encrypted_value in rows:
                prefix = encrypted_value[:3] if len(encrypted_value) >= 3 else b"UNK"
                # v20 prefix = App-Bound Encrypted (Chrome 127+ Windows/Mac confirmed ABE)
                if encrypted_value[:3] == b"v20":
                    abe_confirmed = True
                logger.debug(f"Attempting to decrypt cookie '{name}' (prefix: {prefix!r}) for domain '{host_key}'...")
                
                decrypted = decrypt_chromium_cookie(encrypted_value, db_path)
                
                if decrypted:
                    logger.info(f"✅ SUCCESS: Successfully decrypted cookie '{name}' (prefix: {prefix!r}) for '{host_key}'")
                    decryption_success = True
                    break
                logger.debug(f"Failed to decrypt cookie '{name}' (prefix: {prefix!r}) for '{host_key}'")
                    
            if decryption_success:
                success_count += 1
            else:
                logger.error(f"❌ FAILED: Could not decrypt any cookies in {profile_dir.name}")
                # Provide a specific diagnosis
                if abe_confirmed:
                    logger.error("   Cause: v20 prefix detected — App-Bound Encryption (ABE) is ACTIVE")
                elif abe_suspected:
                    logger.error("   Cause: ABE suspected (no os_crypt in Local State, standard key derivation blocked)")
                else:
                    logger.error("   Cause: Unknown — Keychain key may be wrong or cookies use a different format")
                _print_abe_workaround(sys_os)
                    
            conn.close()
            
        except sqlite3.OperationalError as e:
            logger.error(f"SQLite Error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try: os.remove(temp_path)
                except Exception: pass

    logger.info("=" * 50)
    if success_count == 0:
        logger.error(f"Check Complete. Successfully verified {success_count}/{len(all_targets)} profiles.")
        logger.error("Chrome cookie decryption is NOT working on this system.")
        logger.error("See docs/troubleshooting.md for workarounds.")
    else:
        logger.info(f"Check Complete. Successfully verified {success_count}/{len(all_targets)} profiles. ✅")


def _print_abe_workaround(sys_os: str):
    """Print a targeted workaround message depending on platform."""
    logger.warning("")
    logger.warning("  ─── App-Bound Encryption (ABE) Workarounds ───")
    if sys_os == "Darwin":
        logger.warning("  1. [Recommended] Use Safari — no encryption, works out of the box.")
        logger.warning("     Log into claude.ai / opencode.ai in Safari and Runway will find the cookies.")
        logger.warning("  2. Use environment variables instead:")
        logger.warning("       export CLAUDE_CODE_OAUTH_TOKEN=<token>")
        logger.warning("       export OLLAMA_SESSION_TOKEN=<token>")
        logger.warning("  Note: 'defaults write ApplicationBoundEncryptionEnabled' does NOT work")
        logger.warning("        on macOS Chrome 127+ — it is a Windows-only policy.")
    else:  # Windows / Linux
        logger.warning("  1. [Recommended] Use Firefox — no ABE, cookies are accessible directly.")
        logger.warning("     Log into claude.ai / opencode.ai in Firefox and Runway will find the cookies.")
        logger.warning("  2. Use environment variables instead:")
        logger.warning("       set CLAUDE_CODE_OAUTH_TOKEN=<token>")
        logger.warning("       set OLLAMA_SESSION_TOKEN=<token>")
    logger.warning("  See docs/troubleshooting.md for full details.")
    logger.warning("")


if __name__ == "__main__":
    debug_chrome_extraction()
