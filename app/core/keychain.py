import subprocess
import platform
import threading
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Global cache for keychain secrets to avoid multiple macOS prompts during a session
_KEYCHAIN_CACHE: Dict[str, str] = {}
_KEYCHAIN_LOCK = threading.Lock()

def get_keychain_secret(service: str, account: Optional[str] = None) -> Optional[str]:
    """
    Fetch a secret from the macOS Keychain with in-memory caching.
    Ensures that the user is only prompted once per unique secret per session.
    """
    if platform.system() != "Darwin":
        return None

    cache_key = f"{service}:{account}" if account else service
    
    with _KEYCHAIN_LOCK:
        if cache_key in _KEYCHAIN_CACHE:
            return _KEYCHAIN_CACHE[cache_key]

    try:
        cmd = ["security", "find-generic-password", "-s", service]
        if account:
            cmd.extend(["-a", account])
        cmd.append("-w")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10, # Longer timeout for user interaction
        )

        if result.returncode == 0:
            secret = result.stdout.strip()
            with _KEYCHAIN_LOCK:
                _KEYCHAIN_CACHE[cache_key] = secret
            return secret
        else:
            logger.debug(f"Keychain lookup failed for {service}: {result.stderr.strip()}")
            return None
    except Exception as e:
        logger.error(f"Error accessing macOS Keychain for {service}: {e}")
        return None

def clear_keychain_cache():
    """Clear the in-memory cache."""
    with _KEYCHAIN_LOCK:
        _KEYCHAIN_CACHE.clear()
