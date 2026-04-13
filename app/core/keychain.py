import json
import logging
import os
import platform
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Global cache for keychain secrets to avoid multiple macOS prompts during a session
# We now also cache failures (None) to avoid prompt-spam in a single session
_KEYCHAIN_CACHE: dict[str, str | None] = {}
_KEYCHAIN_LOCK = threading.Lock()

def _get_backoff_file() -> Path:
    """Get path to the persistent keychain backoff file."""
    from app.core.config import get_platform_config_dir
    config_dir = Path(get_platform_config_dir("runway"))
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / ".keychain_backoff.json"

def _is_in_backoff(service: str) -> bool:
    """Check if a service is currently in a 6-hour denial cooldown."""
    backoff_file = _get_backoff_file()
    if not backoff_file.exists():
        return False
    
    try:
        with open(backoff_file) as f:
            backoffs = json.load(f)
        
        last_denied = backoffs.get(service)
        if last_denied:
            denied_dt = datetime.fromisoformat(last_denied)
            # 6-hour cooldown period (matches Swift logic)
            if datetime.now(UTC) < denied_dt + timedelta(hours=6):
                return True
    except Exception:
        pass
    return False

def _record_denial(service: str):
    """Record a persistent denial timestamp for a service."""
    backoff_file = _get_backoff_file()
    try:
        backoffs = {}
        if backoff_file.exists():
            with open(backoff_file) as f:
                backoffs = json.load(f)
        
        backoffs[service] = datetime.now(UTC).isoformat()
        with open(backoff_file, "w") as f:
            json.dump(backoffs, f)
    except Exception:
        pass

def get_keychain_secret(service: str, account: str | None = None, force_refresh: bool = False) -> str | None:
    """
    Fetch a secret from the macOS Keychain with in-memory caching and persistent backoff.
    Ensures that the user is not spammed with prompts if they deny access.
    """
    if platform.system() != "Darwin":
        return None

    # Check for prompt mode override (from settings)
    from app.core.config import settings
    if settings.KEYCHAIN_PROMPT_MODE == "never" and not force_refresh:
        return None

    cache_key = f"{service}:{account}" if account else service
    
    # 1. Check in-memory cache first (including cached failures)
    if not force_refresh:
        with _KEYCHAIN_LOCK:
            if cache_key in _KEYCHAIN_CACHE:
                return _KEYCHAIN_CACHE[cache_key]
                
        # 2. Check persistent 6-hour backoff (to avoid prompt spam across restarts)
        if _is_in_backoff(service):
            logger.debug(f"⏭️ Skipping Keychain prompt for {service} due to active 6-hour backoff")
            return None
    else:
        logger.debug(f"🔄 Bypassing cache for keychain lookup: {service}")

    start_time = time.time()
    try:
        logger.info(f"🔑 Requesting macOS Keychain access for service: {service}...")
        
        # We try -w (password only) first as it is the most standard
        cmd = ["security", "find-generic-password", "-s", service]
        if account:
            cmd.extend(["-a", account])
        cmd.append("-w")

        # Set environment to allow interaction
        env = os.environ.copy()
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=env
        )

        duration_ms = int((time.time() - start_time) * 1000)
        
        if result.returncode == 0:
            secret = result.stdout.strip()
            if secret:
                logger.debug(f"⏱️ Keychain query for {service} took {duration_ms}ms")
                with _KEYCHAIN_LOCK:
                    _KEYCHAIN_CACHE[cache_key] = secret
                return secret
        
        # If -w fails, try with -g (which often puts password in stderr)
        cmd_g = ["security", "find-generic-password", "-s", service]
        if account: cmd_g.extend(["-a", account])
        cmd_g.append("-g")
        
        result_g = subprocess.run(cmd_g, capture_output=True, text=True, timeout=30, env=env)
        
        duration_ms = int((time.time() - start_time) * 1000)

        if result_g.returncode == 0:
            output = result_g.stdout + result_g.stderr
            import re
            match = re.search(r'password: "(.*)"', output)
            if match:
                secret = match.group(1)
                logger.debug(f"⏱️ Keychain query (-g) for {service} took {duration_ms}ms")
                with _KEYCHAIN_LOCK:
                    _KEYCHAIN_CACHE[cache_key] = secret
                return secret

        # Log specific reason for failure
        err = result.stderr.strip() or result_g.stderr.strip()
        if duration_ms > 1000:
            logger.info(f"⚠️ Slow Keychain query: {duration_ms}ms (Likely user interaction or timeout)")

        if "The specified item could not be found" in err:
            logger.debug(f"Keychain service '{service}' not found.")
        elif "User interaction is not allowed" in err:
            logger.warning(f"❌ Keychain access denied: User interaction not allowed for '{service}'.")
        else:
            # Assume any other failure is a user denial or permission issue
            logger.warning(f"❌ Keychain access denied or failed for {service} (Code {result.returncode})")
            _record_denial(service)
            
        # Cache the failure in memory for this session
        with _KEYCHAIN_LOCK:
            _KEYCHAIN_CACHE[cache_key] = None
        
        return None
    except Exception as e:
        logger.error(f"Error accessing macOS Keychain for {service}: {e}")
        return None

def clear_keychain_cache():
    """Clear the in-memory cache."""
    with _KEYCHAIN_LOCK:
        _KEYCHAIN_CACHE.clear()
