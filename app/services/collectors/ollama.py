"""
Ollama Cloud quota collector.

Collection Strategies:
1. ``api`` (PRIMARY, when an API key is available):
   - Bearer `Authorization: Bearer <key>` against
     `GET https://ollama.com/api/usage`.
   - Returns structured monthly quota (``limits.monthly.usage`` plus a
     per-model breakdown and ``activity.period`` reset window). Cleaner
     than the HTML scrape and unaffected by WorkOS redesign churn.

2. ``web`` (FALLBACK, when only a session cookie is available):
   - Scrape `https://ollama.com/settings`.
   - Parses WorkOS "Included usage" meters and pre-rename "Cloud Usage"
     blocks for window quotas; falls back to legacy labeled blocks
     ("Session usage", "Hourly usage", "Weekly usage").
   - Extracts plan name and account email.
"""

import asyncio
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.config import settings
from app.core.date_utils import parse_iso8601_utc
from app.core.utils import (
    HealthCalculator,
    PaceCalculator,
    error_card,
    http_request_with_retry,
    human_delta,
    scrub_log,
)
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class OllamaCollector(BaseCollector):
    PROVIDER_ID = "ollama"
    DEFAULT_WINDOW_TYPE = "session"

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        "api": ("Ollama API key", "_get_ollama_api"),
        "web": ("Web session (scrape)", "_get_ollama_web"),
    }

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
    RE_PLAN_NAME = re.compile(
        r"(?:Cloud Usage|Included usage)\s*</span>\s*<span[^>]*>([^<]+)</span\s*>"
    )
    RE_PLAN_NAME_FALLBACK = re.compile(r"<span[^>]*capitalize[^>]*>([^<]+)</span\s*>")
    RE_EMAIL = re.compile(r'id="header-email"[^>]*>([^<]+)<')
    RE_PERCENT_USED = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*used", re.IGNORECASE)
    RE_PERCENT_REMAINING = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*remaining", re.IGNORECASE)
    RE_WIDTH = re.compile(r"width:\s*([0-9]+(?:\.[0-9]+)?)%", re.IGNORECASE)
    RE_DATA_TIME = re.compile(r'data-time="([^"]+)"')
    # WorkOS redesign: usage lives in <div data-usage-track aria-label="Free usage 0% used">
    RE_USAGE_TRACK_TAG = re.compile(r"<[^>]*\bdata-usage-track\b[^>]*>", re.IGNORECASE)
    RE_ARIA_LABEL = re.compile(r'aria-label="([^"]+)"', re.IGNORECASE)

    # Meter label → window_type. Strong keywords win outright; labels with no
    # keyword fall back to the reset horizon.
    WINDOW_TYPE_KEYWORDS = (
        ("hourly", "session"),
        ("session", "session"),
        ("weekly", "weekly"),
        ("daily", "daily"),
        ("monthly", "monthly"),
    )
    # Vague plan-name keywords only apply when no concrete reset is available.
    WINDOW_TYPE_WEAK_KEYWORDS = (("free", "monthly"),)

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
        "invalid_api_key": "auth_failed",  # pragma: allowlist secret
        "not_logged_in": "auth_required",
        "missing_data": "parse_error",
        "invalid_credential_type": "invalid_config",
    }
    ERROR_MESSAGES = {
        "invalid_api_key": "Ollama API key was rejected. Check or replace the key in provider settings.",  # pragma: allowlist secret
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

    def __init__(
        self,
        account_id: str | None = None,
        account_label: str | None = None,
        credential_account_id: str | None = None,
    ):
        super().__init__(account_id=account_id, account_label=account_label)
        self.credential_account_id = credential_account_id or account_id or "default"
        self.target_url = "https://ollama.com/settings"
        self.labels = ["Session usage", "Hourly usage", "Weekly usage"]
        self._last_error_reason: str = "unknown"
        self._current_input_source: str = "server"
        self._no_monthly_cap = False

    def _is_error_result(self, results: list[dict[str, Any]]) -> bool:
        """Treat an API-confirmed no-cap response as a successful empty result."""
        if self._no_monthly_cap and not results:
            return False
        return super()._is_error_result(results)

    @property
    def successful_empty_result(self) -> bool:
        """The API can confirm an account has no monthly cap and no usage cards."""
        return self._no_monthly_cap

    async def collect(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Clear per-collection state before trying the configured strategies."""
        self._no_monthly_cap = False
        return await super().collect(client)

    async def is_configured(self) -> bool:
        """Ollama is configured when either an API key or a session cookie is cached."""
        if credential_provider.get_provider_api_key(
            "ollama", account_id=self.credential_account_id
        ):
            return True
        if self.credential_account_id == "default" and settings.OLLAMA_API_KEY:
            return True
        tokens = await token_cache.get("ollama", account_id=self.credential_account_id)
        if tokens and tokens.get("api_key"):
            return True
        return self._is_valid_credential(await self._get_cookie_header())

    async def reset(self):
        """Reset collector state between collection runs."""
        self._last_error_reason = "unknown"
        self._no_monthly_cap = False

    def _set_error_reason(self, reason: str) -> None:
        """Keep the most specific auth diagnosis across API and web fallback."""
        priority = {
            "unknown": 0,
            "missing_data": 1,
            "not_logged_in": 2,
            "invalid_credential_type": 3,
            "invalid_api_key": 4,
        }
        if priority.get(reason, 0) >= priority.get(self._last_error_reason, 0):
            self._last_error_reason = reason

    def _wrap_cookie(self, token: str) -> str:
        """Turn a bare cookie value into a usable Cookie header.

        The WorkOS migration serves the same value under both `session` and
        `__Secure-session`; only the latter is accepted. A value without a
        recognized cookie name is therefore emitted under both names.
        """
        if self.RE_COOKIE_PATTERN.search(token):
            return token
        # Any `;` means a multi-pair Cookie header (possibly with nameless
        # pairs like `a=1; flag`) — never a bare value, so never wrap it.
        if ";" in token:
            return token
        return f"session={token}; __Secure-session={token}"

    async def _get_api_key(self) -> str | None:
        """Resolve config, sidecar, then server environment credentials."""
        self._current_input_source = "unknown"
        key = credential_provider.get_provider_api_key(
            "ollama", account_id=self.credential_account_id
        )
        if key:
            self._current_input_source = "config"
            return key.strip()
        cached = await token_cache.get_with_metadata(
            "ollama", account_id=self.credential_account_id
        )
        if cached and cached[0].get("api_key"):
            self._current_input_source = (
                "config" if cached[1].get("source") in ("config", "manual_config") else "sidecar"
            )
            return cached[0]["api_key"].strip()
        if self.credential_account_id == "default" and settings.OLLAMA_API_KEY:
            self._current_input_source = "server"
            return settings.OLLAMA_API_KEY.strip()
        return None

    async def _get_cookie_header(self) -> str | None:
        """Combine session cookies (including chunked ones) into a header string."""
        # 1. DB-stored session cookie (manual override set via settings UI)
        self._current_input_source = "unknown"
        db_token = credential_provider.get_provider_session_cookie(
            "ollama", account_id=self.credential_account_id
        )
        if db_token:
            self._current_input_source = "config"
            return self._wrap_cookie(db_token.strip())

        # 2. Sidecar-pushed cookie via token cache (browser scraping moved to sidecar)
        # Sidecar rules store under `cookie_session`; settings UI uses `session_cookie`.
        cached = await token_cache.get_with_metadata(
            "ollama", account_id=self.credential_account_id
        )
        tokens = cached[0] if cached else None
        cookie = (tokens.get("session_cookie") or tokens.get("cookie_session")) if tokens else None
        if cookie:
            source = cached[1].get("source") if cached else None
            self._current_input_source = (
                "config" if source in ("config", "manual_config") else "sidecar"
            )
            return self._wrap_cookie(cookie.strip())

        # 3. Server environment is the default-account fallback.
        env_token = settings.OLLAMA_SESSION_TOKEN
        if env_token and self.credential_account_id == "default":
            self._current_input_source = "server"
            return self._wrap_cookie(env_token.strip())

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

        # Check every pair's value: a wrapped bare token ("session=<value>") must
        # still trip the API-key guard, not just a bare value.
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            candidate = value or name
            if self.RE_API_KEY_PATTERN.match(candidate):
                logger.debug("Ollama: API key format detected instead of session cookie")
                self._set_error_reason("invalid_credential_type")
                return False

        return self.RE_COOKIE_PATTERN.search(header) is not None

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """API key path is preferred when available — cleaner than HTML scrape
        and unaffected by WorkOS redesign churn."""
        return await self._get_ollama_api(client)

    def _fallback_strategies(self) -> list[Any]:
        """Cookie scrape is the fallback when no API key is configured."""
        return []

    async def _get_ollama_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Bearer-token path: GET https://ollama.com/api/usage."""
        api_key = await self._get_api_key()
        if not api_key:
            return []  # No API key — base collector falls through to ``web``.
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "runway-ai-usage-tracker/1.0",
        }
        try:
            resp = await http_request_with_retry(
                client,
                "GET",
                "https://ollama.com/api/usage",
                headers=headers,
                timeout=self.TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            logger.warning("Ollama API fetch timed out: %s", scrub_log(str(exc)))
            return []
        except Exception as exc:
            logger.warning("Ollama API fetch failed: %s", scrub_log(str(exc)))
            return []
        if resp.status_code in (401, 403):
            self._set_error_reason("invalid_api_key")
            return []
        if resp.status_code != 200:
            return []
        try:
            body = resp.json()
        except Exception:
            self._set_error_reason("missing_data")
            return []
        if not isinstance(body, dict):
            self._set_error_reason("missing_data")
            return []
        return self._build_cards_from_api_usage(body)

    def _build_cards_from_api_usage(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Build a monthly quota card from `/api/usage`'s ``limits`` / ``activity`` shape.

        The shape observed live (Sep 2026):

            {
              "activity": {
                "cost": "0.00000",
                "period": {"type": "last_4_weeks",
                           "starting_at": "2026-08-31T00:00:00Z",
                           "ending_at":   "2026-09-24T22:29:17Z"},
                "models": [...],
              },
              "limits": {"monthly": {"usage": 0.002,
                                      "models": [{"name": "web search",
                                                  "request_count": 1}]}},
            }

        ``limits.monthly.usage`` is a fraction from 0 to 1. The API does not
        provide a reset time, so the card reports percent used without
        inventing reset or pace information. Plans without a monthly cap
        (free tier) report no ``limits.monthly``;
        that case degrades gracefully into an empty card list rather than
        a parse error.
        """
        self._no_monthly_cap = False
        limits = body.get("limits") or {}
        if not isinstance(limits, dict):
            self._set_error_reason("missing_data")
            return []
        # No monthly entry is a valid no-cap plan (for example, free tier).
        # It is a successful empty result, not malformed quota data.
        if "monthly" not in limits or limits["monthly"] is None:
            self._no_monthly_cap = True
            return []
        monthly = limits["monthly"]
        if not isinstance(monthly, dict):
            self._set_error_reason("missing_data")
            return []
        usage_raw = monthly.get("usage")
        if usage_raw is None:
            self._set_error_reason("missing_data")
            return []
        try:
            usage = float(usage_raw)
        except (TypeError, ValueError):
            self._set_error_reason("missing_data")
            return []
        if not math.isfinite(usage) or not 0 <= usage <= 1:
            self._set_error_reason("missing_data")
            return []
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        pct = usage * 100.0
        card = {
            "service_name": "Ollama",
            "icon": "🦙",
            "remaining": f"{100 - pct:.1f}%",
            "unit": "remaining",
            "reset": "—",
            "health": HealthCalculator.from_percentage(pct),
            "pace": "—",
            "detail": f"{pct:.1f}% used · Ollama API",
            "used_value": pct,
            "limit_value": 100.0,
            "pct_used": pct,
            "is_unlimited": False,
            "unit_type": "percent",
            "reset_at": None,
            "account_label": self.account_label or "",
            "window_type": "monthly",
            "provider_id": "ollama",
            "tier": "cloud",
            "data_source": self.DATA_SOURCE_API,
            "input_source": getattr(self, "_current_input_source", "unknown"),
            "usage_url": "https://ollama.com/settings/billing",
            "updated_at": now_iso,
        }
        return [card]

    async def _get_ollama_web(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Cookie-scrape fallback — WorkOS /settings page parse."""
        cookie_header = await self._get_cookie_header()
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
                self._set_error_reason("not_logged_in")
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
            self._set_error_reason("not_logged_in")
            return []  # Empty list signals "not logged in" to error handler

        now = datetime.now(UTC)

        blocks = self._get_usage_blocks(html, now)
        if not blocks:
            logger.debug("Ollama: no usage data found in response")
            self._set_error_reason("missing_data")
            return []

        # If we get here, we have data - parse normally

        # 1. Extract Plan Name
        # The badge sits next to the usage heading:
        #   <span>Included usage</span> <span class="... capitalize">free</span>
        # (legacy markup: "Cloud Usage"). Search a small window after the heading
        # to avoid matching usage blocks.
        plan_name = None
        heading_idx = html.find("Cloud Usage")
        if heading_idx == -1:
            heading_idx = html.find("Included usage")
        if heading_idx != -1:
            plan_window = html[heading_idx : heading_idx + self.WINDOW_PLAN]
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
                token_cache.update_account_metadata(
                    "ollama", self.credential_account_id, name=email
                )
            )
            self.account_label = email

        # 3. Build cards using already-parsed blocks (session/weekly first)
        order = {"session": 0, "weekly": 1}
        blocks.sort(key=lambda block: order.get(block["window_type"], 2))
        return [
            self._make_card("Ollama", block["window_type"], block, plan_name, email, now)
            for block in blocks
        ]

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
                    resets_at = parse_iso8601_utc(raw_date)
                except ValueError:
                    logger.debug("Failed to parse Ollama reset date %r", raw_date, exc_info=True)

            return {"used_percent": pct, "resets_at": resets_at}
        return None

    def _get_usage_blocks(self, html: str, now: datetime) -> list[dict[str, Any]]:
        """Parse every usage meter on the page.

        The WorkOS redesign replaced labeled usage blocks with `data-usage-track`
        meters whose aria-label carries the percentage; the label loop remains as
        the fallback for the pre-rename markup.
        """
        blocks = self._get_meter_blocks(html, now)
        if blocks:
            return blocks
        return self._get_label_blocks(html)

    def _parse_percent(self, text: str) -> float | None:
        pct_match = self.RE_PERCENT_USED.search(text)
        if pct_match:
            return float(pct_match.group(1))
        # "% remaining" and bar width both describe remaining capacity — invert
        pct_match = self.RE_PERCENT_REMAINING.search(text)
        if pct_match:
            return 100.0 - float(pct_match.group(1))
        pct_match = self.RE_WIDTH.search(text)
        if pct_match:
            return 100.0 - float(pct_match.group(1))
        return None

    def _parse_reset(self, text: str) -> datetime | None:
        date_match = self.RE_DATA_TIME.search(text)
        if not date_match:
            return None
        raw_date = date_match.group(1)
        try:
            return parse_iso8601_utc(raw_date)
        except ValueError:
            logger.debug("Failed to parse Ollama reset date %r", raw_date, exc_info=True)
            return None

    def _window_type_for(self, label: str, resets_at: datetime | None, now: datetime) -> str:
        lower_label = label.lower()
        for keyword, window_type in self.WINDOW_TYPE_KEYWORDS:
            if keyword in lower_label:
                return window_type
        # A concrete reset beats a vague plan-name keyword ("Free usage")
        if resets_at is not None:
            horizon = resets_at - now
            if horizon <= timedelta(days=1.5):
                return "daily"
            if horizon <= timedelta(days=8):
                return "weekly"
            if horizon <= timedelta(days=35):
                return "monthly"
            return "rolling"
        for keyword, window_type in self.WINDOW_TYPE_WEAK_KEYWORDS:
            if keyword in lower_label:
                return window_type
        return self.DEFAULT_WINDOW_TYPE

    def _get_meter_blocks(self, html: str, now: datetime) -> list[dict[str, Any]]:
        """Parse WorkOS `data-usage-track` meters (aria-label carries the %)."""
        blocks: list[dict[str, Any]] = []
        seen_types: set[str] = set()

        for tag_match in self.RE_USAGE_TRACK_TAG.finditer(html):
            label_match = self.RE_ARIA_LABEL.search(tag_match.group(0))
            if not label_match:
                continue
            label = label_match.group(1).strip()

            # aria-label is authoritative; the visible text is only a fallback
            window = html[tag_match.end() : tag_match.end() + self.WINDOW_USAGE]
            pct = self._parse_percent(label)
            if pct is None:
                pct = self._parse_percent(window)
            if pct is None:
                continue

            resets_at = self._parse_reset(window)
            window_type = self._window_type_for(label, resets_at, now)
            if window_type in seen_types:
                logger.debug(
                    "Ollama: skipping meter %r — window_type %r already collected",
                    label,
                    window_type,
                )
                continue
            seen_types.add(window_type)
            blocks.append({"used_percent": pct, "resets_at": resets_at, "window_type": window_type})

        return blocks

    def _get_label_blocks(self, html: str) -> list[dict[str, Any]]:
        """Parse legacy labeled blocks ("Session usage" / "Weekly usage" + bar)."""
        blocks: list[dict[str, Any]] = []
        label_types = (
            ("Session usage", "session"),
            ("Hourly usage", "session"),
            ("Weekly usage", "weekly"),
        )

        for label, window_type in label_types:
            if any(block["window_type"] == window_type for block in blocks):
                continue
            idx = html.find(label)
            if idx == -1:
                continue

            window = html[idx : idx + self.WINDOW_USAGE]
            pct = self._parse_percent(window)
            if pct is None:
                continue

            blocks.append(
                {
                    "used_percent": pct,
                    "resets_at": self._parse_reset(window),
                    "window_type": window_type,
                }
            )

        return blocks

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

    async def _error_handler(self) -> list[dict[str, Any]]:
        error_type = self.ERROR_TYPE_MAP.get(self._last_error_reason, "unknown")
        message = self.ERROR_MESSAGES.get(
            self._last_error_reason, "Not logged in or parsing failed"
        )

        return [error_card("Ollama Cloud", "🦙", message, error_type=error_type)]
