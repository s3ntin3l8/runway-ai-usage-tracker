import asyncio
import base64
import json
import logging
import os
import random
import re
import tempfile
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class IdentityExtractor:
    """Helper for extracting user identity from various sources (JWT, logs, etc.)"""

    @staticmethod
    def extract_jwt_payload(token: str) -> dict[str, Any]:
        """Robustly decode JWT payload without external libraries."""
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return {}

            payload_b64 = parts[1]
            # Fix padding
            padding = len(payload_b64) % 4
            if padding:
                payload_b64 += "=" * (4 - padding)

            return json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
        except Exception:
            return {}

    @classmethod
    def get_email_from_jwt(cls, token: str) -> str | None:
        """Extract email claim from JWT payload."""
        payload = cls.extract_jwt_payload(token)
        return payload.get("email")

    @classmethod
    def get_client_id_from_jwt(cls, token: str) -> str | None:
        """Extract azp or aud claim from JWT payload."""
        payload = cls.extract_jwt_payload(token)
        return payload.get("azp") or payload.get("aud")

    @staticmethod
    def extract_best_email(emails: list[dict[str, Any]]) -> str | None:
        """
        Standardized logic to extract the 'best' email from a list of provider emails.
        Prioritizes:
        1. Verified, non-noreply email addresses.
        2. Primary email (even if it's a noreply address).
        3. First available email.
        """
        if not emails:
            return None

        # 1. Search for a verified, real (non-noreply) email
        for e in emails:
            email = e.get("email")
            if email and e.get("verified") and "noreply.github.com" not in email:
                return email

        # 2. Fallback: Search for the primary email
        for e in emails:
            if e.get("primary"):
                return e.get("email")

        # 3. Last fallback: just return the first one available
        return emails[0].get("email")


class PaceCalculator:
    @staticmethod
    def estimate_longevity(pct_used: float, reset_at: datetime | None) -> str:
        if pct_used <= 0:
            return "Stable"
        if not reset_at:
            return "Sustainable"
        now = datetime.now(UTC)
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=UTC)
        time_to_reset = (reset_at - now).total_seconds()
        if time_to_reset <= 0:
            return "Pending Reset"
        remaining_pct = 100 - pct_used
        if remaining_pct <= 0:
            return "Exhausted"
        if remaining_pct < 10:
            return "Fast Burn"
        if remaining_pct < 30:
            return "Moderate Burn"
        return "Sustainable"


def human_delta(target_dt: datetime | None) -> str:
    if not target_dt:
        return "—"
    now = datetime.now(UTC)
    if target_dt.tzinfo is None:
        target_dt = target_dt.replace(tzinfo=UTC)
    diff = target_dt - now
    seconds = int(diff.total_seconds())
    if seconds < 0:
        return "Just now"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    # NEW: xd yh format for >24h
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    return f"{days}d {hours}h"


def error_card(service: str, icon: str, message: str, error_type: str = "unknown"):
    from app.models.builder import LimitCardBuilder

    return LimitCardBuilder.error(service, icon, message, error_type)


def extract_token_regex(detail: str, prefix: str) -> str | None:
    """
    Robustly extract a token from a detail string using regex.
    Matches the prefix followed by non-whitespace/separator characters.
    """
    pattern = rf"{re.escape(prefix)}\s*([^\s·\[\]]+)"
    match = re.search(pattern, detail)
    return match.group(1) if match else None


class HealthCalculator:
    """Standardized logic for determining card health status."""

    @staticmethod
    def from_percentage(pct_used: float) -> str:
        """
        Map percentage used to a health status string.
        Standard thresholds: >=90 (Critical), >=70 (Warning), else Good.
        """
        if pct_used >= 90:
            return "critical"
        if pct_used >= 70:
            return "warning"
        return "good"

    @staticmethod
    def from_remaining(remaining: float, limit: float) -> str:
        """
        Calculate health status from remaining and limit values.
        Returns 'unknown' if limit is 0 or invalid.
        """
        if limit <= 0:
            return "unknown"
        pct_used = ((limit - remaining) / limit) * 100
        return HealthCalculator.from_percentage(pct_used)

    @staticmethod
    def from_spend(spend: float, limit: float) -> str:
        """
        Calculate health based on spend vs limit (for monthly budgets).
        Critical if limit reached, Warning if < $5 remaining.
        """
        if limit <= 0:
            return "good"
        remaining = limit - spend
        if remaining <= 0:
            return "critical"
        if remaining <= 5.0:
            return "warning"
        return "good"

    @staticmethod
    def from_balance(balance: float) -> str:
        """
        Calculate health based on prepaid balance.
        Critical if $0, Warning if <= $5.
        """
        if balance <= 0:
            return "critical"
        if balance <= 5.0:
            return "warning"
        return "good"


async def http_request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    max_retries: int = 3,
    initial_delay: float = 0.5,
    **kwargs,
) -> httpx.Response:
    """
    Make an HTTP request with exponential backoff retry on 429 (rate limit).
    Respects 'Retry-After' header if present.

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
                logger.warning(
                    f"Rate limited (429) on {method.upper()} {url} after {max_retries} attempts"
                )
                return response

            # Calculate backoff: check Retry-After header first, then exponential with jitter
            retry_after = response.headers.get("Retry-After")
            try:
                if retry_after:
                    if retry_after.isdigit():
                        wait_time = float(retry_after)
                    else:
                        # Handle HTTP-date format (optional but good practice)
                        from email.utils import parsedate_to_datetime

                        retry_date = parsedate_to_datetime(retry_after)
                        wait_time = (retry_date - datetime.now(UTC)).total_seconds()

                    # Add a small buffer
                    wait_time = max(0.1, wait_time + 0.5)
                else:
                    wait_time = (2**attempt) * initial_delay + random.uniform(0, 0.1 * (2**attempt))
            except Exception:
                wait_time = (2**attempt) * initial_delay + random.uniform(0, 0.1 * (2**attempt))

            # CRITICAL: Cap wait time. If it's too long (e.g. 1 hour),
            # don't block the collector task (which now has a 20s timeout).
            if wait_time > 5.0:
                logger.warning(
                    f"Rate limit wait time too long ({wait_time:.1f}s), aborting retries for {method.upper()} {url}"
                )
                return response

            logger.info(
                f"Rate limited (429) on {method.upper()} {url}, retrying in {wait_time:.2f}s (attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(wait_time)

        except Exception as e:
            if attempt == max_retries - 1:
                raise
            # For non-rate-limit errors, log and retry with shorter delay
            logger.warning(f"Request failed on attempt {attempt + 1}: {e}, retrying...")
            await asyncio.sleep(initial_delay * (attempt + 1))

    # This shouldn't be reached but just in case
    raise RuntimeError(f"Max retries ({max_retries}) exceeded for {method.upper()} {url}")


def safe_write_json(path: str, data: dict):
    """
    Write JSON data to a file atomically using a temporary file and rename.
    This prevents file corruption if the process is interrupted during writing.
    """
    # ensure directory exists
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Use the same directory for the temp file to ensure os.replace is atomic (same filesystem)
    fd, temp_path = tempfile.mkstemp(
        dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # Ensure data is written to disk

        # Atomic rename
        os.replace(temp_path, path)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.error(f"Failed to write JSON atomically to {path}: {e}")
        raise
