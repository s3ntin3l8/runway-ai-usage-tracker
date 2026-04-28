"""
Ollama Cloud quota collector.

Collection Strategy:
1. Primary: Scrape https://ollama.com/settings
   - Requires session cookie from environment (OLLAMA_SESSION_TOKEN) or browser.
   - Parses Cloud Usage section for session and weekly quotas.
   - Extracts plan name, account email, usage percentages, and reset timestamps.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.browser_cookies import get_session_cookies
from app.core.config import settings
from app.core.utils import (
    HealthCalculator,
    PaceCalculator,
    error_card,
    http_request_with_retry,
    human_delta,
)
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class OllamaCollector(BaseCollector):
    PROVIDER_ID = "ollama"
    DEFAULT_WINDOW_TYPE = "session"

    RECOGNIZED_COOKIE_NAMES = (
        "session",
        "ollama_session",
        "__Host-ollama_session",
        "__Secure-session",
        "__Secure-next-auth.session-token",
        "next-auth.session-token",
        "access-token",
    )

    # Pre-compiled regex patterns for performance
    RE_PLAN_NAME = re.compile(r"Cloud Usage\s*</span>\s*<span[^>]*>([^<]+)</span>")
    RE_PLAN_NAME_FALLBACK = re.compile(r"<span[^>]*capitalize[^>]*>([^<]+)</span>")
    RE_EMAIL = re.compile(r'id="header-email"[^>]*>([^<]+)<')
    RE_PERCENT_USED = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*used", re.IGNORECASE)
    RE_PERCENT_REMAINING = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*remaining", re.IGNORECASE)
    RE_WIDTH = re.compile(r"width:\s*([0-9]+(?:\.[0-9]+)?)%", re.IGNORECASE)
    RE_DATA_TIME = re.compile(r'data-time="([^"]+)"')

    # Patterns for detecting logged-out state (case-insensitive)
    RE_SIGN_IN_HEADING = re.compile(r"sign in to ollama|log in to ollama", re.IGNORECASE)
    RE_AUTH_FORM = re.compile(
        r"<form.*(type=[\"']email[\"']|name=[\"']email[\"']|type=[\"']password[\"']|name=[\"']password[\"'])",
        re.IGNORECASE | re.DOTALL,
    )

    # Detect Ollama API Key patterns (not suitable for web scraping)
    # 1. sk- prefix (standard API key format)
    # 2. 32hex.20+alpha format (observed in cloud tokens)
    RE_API_KEY_PATTERN = re.compile(
        r"^(?:sk-[a-zA-Z0-9]{20,}|(?:[a-fA-F0-9]{32}\.[a-zA-Z0-9]{20,}))$"
    )

    # Magic numbers
    WINDOW_PLAN = 400
    WINDOW_USAGE = 800
    TIMEOUT_SECONDS = 15

    # Error handling
    ERROR_TYPE_MAP = {
        "not_logged_in": "auth_required",
        "missing_data": "parse_error",
        "invalid_credential_type": "invalid_config",
    }
    ERROR_MESSAGES = {
        "not_logged_in": "Not logged in. Please log in at ollama.com",
        "missing_data": "Could not parse usage data",
        "invalid_credential_type": "API Key detected. Quota tracking requires a Session Cookie (ollama_session).",
    }

    # Pre-compiled regex for cookie validation
    RE_COOKIE_PATTERN = re.compile(
        r"(" + "|".join(RECOGNIZED_COOKIE_NAMES) + r")(?:\.|\=)", re.IGNORECASE
    )

    # Static HTTP headers (Cookie is injected per-request)
    STATIC_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Referer": "https://ollama.com",
        "Sec-Ch-Ua": '"Not(A:Bar";v="99", "Google Chrome";v="133", "Chromium";v="133"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)
        self.target_url = "https://ollama.com/settings"
        self.labels = ["Session usage", "Hourly usage", "Weekly usage"]
        self._last_error_reason: str = "unknown"
        self._current_input_source: str = "server"

    async def is_configured(self) -> bool:
        """Check if Ollama session cookie is present."""
        return self._is_valid_credential(self._get_cookie_header())

    async def reset(self):
        """Reset collector state between collection runs."""
        self._last_error_reason = "unknown"

    def _get_cookie_header(self) -> str | None:
        """Combine session cookies (including chunked ones) into a header string."""
        # 1. DB-stored session cookie (manual override set via settings UI)
        db_token = credential_provider.get_provider_session_cookie("ollama")
        if db_token:
            self._current_input_source = "config"
            token = db_token.strip()
            # If the user pasted a string that already contains a recognized cookie name, return as is.
            # Otherwise, default to prepending "session=" for backward compatibility.
            if self.RE_COOKIE_PATTERN.search(token):
                return token
            return f"session={token}"

        # 2. Check environment variable
        env_token = settings.OLLAMA_SESSION_TOKEN
        if env_token:
            self._current_input_source = "server"
            return f"session={env_token}"

        # 3. Check browser cookies for various possible names (ordered by priority)
        for name in self.RECOGNIZED_COOKIE_NAMES:
            # get_session_cookies returns a list (handles chunked .0, .1, etc.)
            cookies = get_session_cookies("ollama.com", name)
            if cookies:
                self._current_input_source = "server"
                # Join chunked cookies: "name=val0; name.0=val0; name.1=val1..."
                # Actually NextAuth typically uses the base name for the first chunk if small,
                # or name.0, name.1 if large.
                # The Swift code joins them with "; "
                header_parts = []
                if len(cookies) == 1:
                    header_parts.append(f"{name}={cookies[0]}")
                else:
                    for i, val in enumerate(cookies):
                        header_parts.append(f"{name}.{i}={val}")

                return "; ".join(header_parts)

        return None

    def _looks_signed_out(self, html: str) -> bool:
        """Check if the HTML indicates the user is not logged in."""
        if self.RE_SIGN_IN_HEADING.search(html):
            return True

        if self.RE_AUTH_FORM.search(html):
            auth_routes = ["/api/auth/signin", "/auth/signin", "/signin", "/login"]
            if any(route in html for route in auth_routes):
                return True

        return False

    def _validate_cookie_header(self, header: str | None) -> bool:
        """Validate that cookie header contains a recognized session cookie name."""
        if not header:
            return False

        # Extract only the value if it's "session=value"
        clean_value = header
        if header.startswith("session="):
            clean_value = header[len("session=") :]

        # Check if the value looks like an API key instead of a session cookie
        if self.RE_API_KEY_PATTERN.search(clean_value):
            logger.debug("Ollama: API key format detected instead of session cookie")
            self._last_error_reason = "invalid_credential_type"
            return False

        return self.RE_COOKIE_PATTERN.search(header) is not None

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Scrape Ollama settings page."""
        cookie_header = self._get_cookie_header()
        if not cookie_header:
            return []

        # Validate cookie header before making request (fail fast)
        if not self._validate_cookie_header(cookie_header):
            logger.debug("Cookie header missing recognized session cookie")
            return []

        # Merge static headers with dynamic Cookie
        headers = {**self.STATIC_HEADERS, "Cookie": cookie_header}

        try:
            # CRITICAL: set follow_redirects=True as Ollama often redirects to www. or /
            resp = await http_request_with_retry(
                client,
                "GET",
                self.target_url,
                headers=headers,
                timeout=self.TIMEOUT_SECONDS,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                return self._parse_html(resp.text)
            if resp.status_code in (401, 403):
                logger.debug("Ollama auth failed (401/403)")
            else:
                logger.debug(f"Ollama fetch failed with status {resp.status_code} at {resp.url}")
        except Exception as e:
            logger.debug(f"Ollama fetch error: {e}")

        return []

    def _parse_html(self, html: str) -> list[dict[str, Any]]:
        """Parse the settings page HTML for usage data.

        Returns:
            Empty list if not logged in (signals to try fallback/error handler).
            Cards list if usage data found.
        """
        # Check if user is logged out
        if self._looks_signed_out(html):
            logger.debug("Ollama: user is not logged in")
            self._last_error_reason = "not_logged_in"
            return []  # Empty list signals "not logged in" to error handler

        # Check if no usage blocks found (but not logged out)
        # Parse both blocks in one pass to avoid double html.find() calls
        session_block, weekly_block = self._get_usage_blocks(html)

        if not session_block and not weekly_block:
            logger.debug("Ollama: no usage data found in response")
            self._last_error_reason = "missing_data"
            return []

        # If we get here, we have data - parse normally
        now = datetime.now(UTC)

        # 1. Extract Plan Name
        # The badge sits near "Cloud Usage": <span class="... capitalize">free</span>
        # Search within a small window after "Cloud Usage" to avoid matching usage blocks.
        plan_name = None
        cu_idx = html.find("Cloud Usage")
        if cu_idx != -1:
            plan_window = html[cu_idx : cu_idx + self.WINDOW_PLAN]
            # Try specific pattern first (Swift approach), fallback to capitalize class
            plan_match = self.RE_PLAN_NAME.search(plan_window)
            if not plan_match:
                plan_match = self.RE_PLAN_NAME_FALLBACK.search(plan_window)
            if plan_match:
                plan_name = plan_match.group(1).strip()

        # 2. Extract Account Email
        email = None
        email_match = self.RE_EMAIL.search(html)
        if email_match:
            email = email_match.group(1).strip()

        # Identity Promotion: sync discovered email/name back to the token cache metadata
        if email and self.account_id:
            asyncio.create_task(
                token_cache.update_account_metadata("ollama", self.account_id, name=email)
            )
            self.account_label = email

        # 3. Build cards using already-parsed blocks
        cards = []
        if session_block:
            cards.append(self._make_card("Ollama", "session", session_block, plan_name, email, now))

        if weekly_block:
            cards.append(self._make_card("Ollama", "weekly", weekly_block, plan_name, email, now))

        return cards

    def _get_usage_block(self, labels: list[str], html: str) -> dict[str, Any] | None:
        for label in labels:
            idx = html.find(label)
            if idx == -1:
                continue

            # Take a window of 800 chars after the label
            window = html[idx : idx + self.WINDOW_USAGE]

            # Parse percentage
            pct = None
            # Pattern 1a: "XX% used"
            pct_match = self.RE_PERCENT_USED.search(window)
            if pct_match:
                pct = float(pct_match.group(1))
            else:
                # Pattern 1b: "XX% remaining" → invert to get used %
                pct_match = self.RE_PERCENT_REMAINING.search(window)
                if pct_match:
                    pct = 100.0 - float(pct_match.group(1))
                else:
                    # Pattern 2: width fallback — Ollama bars show *remaining* width, so invert
                    pct_match = self.RE_WIDTH.search(window)
                    if pct_match:
                        pct = 100.0 - float(pct_match.group(1))

            if pct is None:
                continue

            # Parse reset date
            resets_at = None
            date_match = self.RE_DATA_TIME.search(window)
            if date_match:
                raw_date = date_match.group(1)
                try:
                    # ISO 8601 parsing
                    resets_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                except ValueError:
                    pass

            return {"used_percent": pct, "resets_at": resets_at}
        return None

    def _get_usage_blocks(self, html: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Parse both session and weekly usage blocks in one pass.

        Returns:
            Tuple of (session_block, weekly_block)
        """
        session_block = None
        weekly_block = None

        session_labels = ["Session usage", "Hourly usage"]

        for label in session_labels + ["Weekly usage"]:
            idx = html.find(label)
            if idx == -1:
                continue

            window = html[idx : idx + self.WINDOW_USAGE]

            pct = None
            pct_match = self.RE_PERCENT_USED.search(window)
            if pct_match:
                pct = float(pct_match.group(1))
            else:
                pct_match = self.RE_PERCENT_REMAINING.search(window)
                if pct_match:
                    pct = 100.0 - float(pct_match.group(1))
                else:
                    pct_match = self.RE_WIDTH.search(window)
                    if pct_match:
                        pct = 100.0 - float(pct_match.group(1))

            if pct is None:
                continue

            resets_at = None
            date_match = self.RE_DATA_TIME.search(window)
            if date_match:
                raw_date = date_match.group(1)
                try:
                    resets_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                except ValueError:
                    pass

            block = {"used_percent": pct, "resets_at": resets_at}

            if label in session_labels and session_block is None:
                block["window_type"] = "session"
                session_block = block
            elif label == "Weekly usage" and weekly_block is None:
                block["window_type"] = "weekly"
                weekly_block = block

        return session_block, weekly_block

    def _make_card(
        self,
        service_name: str,
        window_type: str,
        block: dict[str, Any],
        plan: str | None,
        email: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        pct = block["used_percent"]
        resets_at = block["resets_at"]

        detail = f"{pct:.1f}% used"
        if plan:
            detail = f"{plan} · {detail}"
        if email:
            detail = f"{detail} · {email}"

        return {
            "service_name": service_name,
            "window_type": window_type,
            "icon": "🦙",
            "remaining": f"{(100 - pct):.1f}%",
            "unit": "remaining",
            "reset": human_delta(resets_at),
            "health": HealthCalculator.from_percentage(pct),
            "pace": PaceCalculator.estimate_longevity(pct, resets_at),
            "detail": detail,
            "used_value": pct,
            "limit_value": 100.0,
            "unit_type": "percent",
            "reset_at": resets_at.isoformat() if resets_at else None,
            "account_label": email,
            "data_source": self.DATA_SOURCE_WEB,
            "input_source": getattr(self, "_current_input_source", "unknown"),
            "tier": plan.lower() if plan else "free",
            "usage_url": self.target_url,
            "updated_at": now.isoformat(),
        }

    def _fallback_strategies(self) -> list[Any]:
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        error_type = self.ERROR_TYPE_MAP.get(self._last_error_reason, "unknown")
        message = self.ERROR_MESSAGES.get(
            self._last_error_reason, "Not logged in or parsing failed"
        )

        return [error_card("Ollama Cloud", "🦙", message, error_type=error_type)]
