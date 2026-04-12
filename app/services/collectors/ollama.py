"""
Ollama Cloud quota collector.

Collection Strategy:
1. Primary: Scrape https://ollama.com/settings
   - Requires session cookie from environment (OLLAMA_SESSION_TOKEN) or browser.
   - Parses Cloud Usage section for session and weekly quotas.
   - Extracts plan name, account email, usage percentages, and reset timestamps.
"""

import re
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import httpx
from app.services.collectors.base import BaseCollector
from app.core.browser_cookies import get_session_cookies
from app.core.utils import PaceCalculator, human_delta, error_card, http_request_with_retry
from app.core.config import settings

logger = logging.getLogger(__name__)


class OllamaCollector(BaseCollector):
    PROVIDER_ID = "ollama"
    DEFAULT_WINDOW_TYPE = "session"

    def __init__(self, account_id: Optional[str] = None, account_label: Optional[str] = None):
        super().__init__(account_id=account_id, account_label=account_label)
        self.target_url = "https://ollama.com/settings"
        self.labels = ["Session usage", "Hourly usage", "Weekly usage"]

    def _get_cookie_header(self) -> Optional[str]:
        """Combine session cookies (including chunked ones) into a header string."""
        # 1. Check environment variable first
        env_token = settings.OLLAMA_SESSION_TOKEN
        if env_token:
            return f"session={env_token}"

        # 2. Check browser cookies for various possible names
        possible_names = [
            "session",
            "ollama_session",
            "__Host-ollama_session",
            "__Secure-next-auth.session-token",
            "__Secure-session",
            "access-token",
        ]
        
        for name in possible_names:
            # get_session_cookies returns a list (handles chunked .0, .1, etc.)
            cookies = get_session_cookies("ollama.com", name)
            if cookies:
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

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Scrape Ollama settings page."""
        cookie_header = self._get_cookie_header()
        if not cookie_header:
            return []

        # Enhanced stealth headers to mimic a real browser
        headers = {
            "Cookie": cookie_header,
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

        try:
            # CRITICAL: set follow_redirects=True as Ollama often redirects to www. or /
            resp = await http_request_with_retry(
                client, 
                "GET", 
                self.target_url, 
                headers=headers, 
                timeout=15,
                follow_redirects=True
            )
            if resp.status_code == 200:
                return self._parse_html(resp.text)
            elif resp.status_code in (401, 403):
                logger.debug("Ollama auth failed (401/403)")
            else:
                logger.debug(f"Ollama fetch failed with status {resp.status_code} at {resp.url}")
        except Exception as e:
            logger.debug(f"Ollama fetch error: {e}")

        return []

    def _parse_html(self, html: str) -> List[Dict[str, Any]]:
        """Parse the settings page HTML for usage data."""
        cards = []
        now = datetime.now(timezone.utc)

        # 1. Extract Plan Name
        plan_name = None
        plan_match = re.search(r'Cloud Usage\s*</span>\s*<span[^>]*>([^<]+)</span>', html, re.DOTALL)
        if plan_match:
            plan_name = plan_match.group(1).strip()

        # 2. Extract Account Email
        email = None
        email_match = re.search(r'id="header-email"[^>]*>([^<]+)<', html)
        if email_match:
            email = email_match.group(1).strip()

        # 3. Parse Usage Blocks
        session_block = self._get_usage_block(["Session usage", "Hourly usage"], html)
        weekly_block = self._get_usage_block(["Weekly usage"], html)

        if session_block:
            cards.append(self._make_card("Ollama Session", session_block, plan_name, email, now))
        
        if weekly_block:
            cards.append(self._make_card("Ollama Weekly", weekly_block, plan_name, email, now))

        return cards

    def _get_usage_block(self, labels: List[str], html: str) -> Optional[Dict[str, Any]]:
        for label in labels:
            idx = html.find(label)
            if idx == -1:
                continue
            
            # Take a window of 800 chars after the label
            window = html[idx : idx + 800]
            
            # Parse percentage
            pct = None
            # Pattern 1: "XX% used"
            pct_match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*%\s*used', window, re.IGNORECASE)
            if pct_match:
                pct = float(pct_match.group(1))
            else:
                # Pattern 2: "width: XX%"
                pct_match = re.search(r'width:\s*([0-9]+(?:\.[0-9]+)?)%', window, re.IGNORECASE)
                if pct_match:
                    pct = float(pct_match.group(1))
            
            if pct is None:
                continue

            # Parse reset date
            resets_at = None
            date_match = re.search(r'data-time="([^"]+)"', window)
            if date_match:
                raw_date = date_match.group(1)
                try:
                    # ISO 8601 parsing
                    resets_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                except ValueError:
                    pass
            
            return {"used_percent": pct, "resets_at": resets_at}
        return None

    def _make_card(self, service_name: str, block: Dict[str, Any], plan: Optional[str], email: Optional[str], now: datetime) -> Dict[str, Any]:
        pct = block["used_percent"]
        resets_at = block["resets_at"]
        
        detail = f"{pct:.1f}% used"
        if plan:
            detail = f"{plan} · {detail}"
        if email:
            detail = f"{detail} · {email}"

        return {
            "service_name": service_name,
            "icon": "🦙",
            "remaining": f"{(100-pct):.1f}%",
            "unit": "remaining",
            "reset": human_delta(resets_at),
            "health": "good" if pct < 80 else "warning" if pct < 95 else "danger",
            "pace": PaceCalculator.estimate_longevity(pct, resets_at),
            "detail": detail,
            "used_value": float(pct),
            "limit_value": 100.0,
            "unit_type": "percent",
            "reset_at": resets_at.isoformat() if resets_at else None,
            "data_source": "web_scrape",
            "tier": plan.lower() if plan else "unknown",
            "usage_url": self.target_url,
            "updated_at": now.isoformat(),
        }

    def _fallback_strategies(self) -> List[Any]:
        return []

    async def _error_handler(self) -> List[Dict[str, Any]]:
        return [
            error_card(
                "Ollama Cloud", "🦙", "Not logged in or parsing failed", error_type="missing_config"
            )
        ]
