from datetime import datetime, timezone
from typing import Optional
import asyncio
import random
import httpx
import logging

logger = logging.getLogger(__name__)

class PaceCalculator:
    @staticmethod
    def estimate_longevity(pct_used: float, reset_at: Optional[datetime]) -> str:
        if pct_used <= 0: return "Stable"
        if not reset_at: return "Sustainable"
        now = datetime.now(timezone.utc)
        if reset_at.tzinfo is None: reset_at = reset_at.replace(tzinfo=timezone.utc)
        time_to_reset = (reset_at - now).total_seconds()
        if time_to_reset <= 0: return "Pending Reset"
        remaining_pct = 100 - pct_used
        if remaining_pct <= 0: return "Exhausted"
        if remaining_pct < 10: return "Fast Burn"
        if remaining_pct < 30: return "Moderate Burn"
        return "Sustainable"

def human_delta(target_dt: Optional[datetime]) -> str:
    if not target_dt: return "—"
    now = datetime.now(timezone.utc)
    if target_dt.tzinfo is None: target_dt = target_dt.replace(tzinfo=timezone.utc)
    diff = target_dt - now
    seconds = int(diff.total_seconds())
    if seconds < 0: return "Just now"
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m"
    if seconds < 86400: return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    # NEW: xd yh format for >24h
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    return f"{days}d {hours}h"

def error_card(service: str, icon: str, message: str):
    return {
        "service": service,
        "icon": icon,
        "remaining": "ERR",
        "unit": "Check State",
        "reset": "—",
        "health": "critical",
        "pace": "Stopped",
        "detail": message
    }

async def http_request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    max_retries: int = 3,
    initial_delay: float = 0.5,
    **kwargs
) -> httpx.Response:
    """
    Make an HTTP request with exponential backoff retry on 429 (rate limit).
    
    Args:
        client: httpx.AsyncClient instance
        method: HTTP method (get, post, etc.)
        url: Request URL
        max_retries: Maximum number of retries (default: 3)
        initial_delay: Initial backoff delay in seconds (default: 0.5)
        **kwargs: Additional arguments to pass to the request
    
    Returns:
        httpx.Response: The successful response or the final failed response
    """
    for attempt in range(max_retries):
        try:
            response = await client.request(method, url, **kwargs)
            
            # If not rate limited, return immediately
            if response.status_code != 429:
                return response
            
            # If this was the last attempt, return the 429 response
            if attempt == max_retries - 1:
                logger.warning(f"Rate limited (429) on {method.upper()} {url} after {max_retries} attempts")
                return response
            
            # Calculate backoff with jitter
            wait_time = (2 ** attempt) * initial_delay + random.uniform(0, 0.1 * (2 ** attempt))
            logger.info(f"Rate limited (429) on {method.upper()} {url}, retrying in {wait_time:.2f}s (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait_time)
            
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            # For non-rate-limit errors, log and retry with shorter delay
            logger.warning(f"Request failed on attempt {attempt + 1}: {e}, retrying...")
            await asyncio.sleep(initial_delay * (attempt + 1))
    
    # This shouldn't be reached but just in case
    raise RuntimeError(f"Max retries ({max_retries}) exceeded for {method.upper()} {url}")
