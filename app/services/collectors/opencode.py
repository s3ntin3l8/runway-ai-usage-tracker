"""
OpenCode quota collector with web API (Chrome cookies) as primary source.

Collection Strategy:
1. OpenCode Web API (PRIMARY)
   - Uses Chrome cookies to authenticate with opencode.ai
   - Calls https://opencode.ai/_server endpoint
   - Returns aggregated usage from ALL devices (web IDE, TUI, etc.)
   - Shows rolling 5-hour and weekly windows

2. Sidecar Aggregation (FALLBACK)
   - Aggregates local DB data from multiple hosts via external metrics
   - Used when web API fails (no Chrome login, cookie decryption fails)
   - Each host runs sidecar script to push local data

Local DB Collection:
- Controlled by LOCAL_COLLECTOR_ENABLED env var
- Only used as additional data source, not primary
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.browser_cookies import get_opencode_session_cookie
from app.core.config import is_local_collector_enabled, settings
from app.core.utils import PaceCalculator, error_card, http_request_with_retry
from app.services.collectors.base import BaseCollector
from app.services.external_metrics import external_metric_service
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

# Matches each inline JS record from the /usage page's embedded $R[N]={...} objects.
# Supports both OLD format (direct) and NEW format (React Suspense wrapped with $R[N]= prefix).
# Fields are in a fixed order so we can match them positionally.
_USAGE_RECORD_RE = re.compile(
    r"\{id:\"usg_[^\"]*\""
    r",workspaceID:\"[^\"]*\""
    r",timeCreated:(?:\$R\[\d+\]=)?new Date\(\"([^\"]+)\"\)"  # G1: ISO timestamp
    r",timeUpdated:(?:\$R\[\d+\]=)?new Date\(\"[^\"]+\"\)"
    r",timeDeleted:[^,]+"
    r',model:"([^"]+)"'  # G2: model name
    r',provider:"([^"]+)"'  # G3: provider
    r",inputTokens:(-?\d+)"  # G4
    r",outputTokens:(-?\d+)"  # G5
    r",reasoningTokens:(-?\d+|null)"  # G6
    r",cacheReadTokens:(-?\d+)"  # G7
    r",cacheWrite5mTokens:(-?\d+|null)"  # G8: nullable
    r",cacheWrite1hTokens:(-?\d+|null)"  # G9: nullable
    r",cost:(-?\d+)"  # G10: cost raw int (÷1e8 = USD)
    r',keyID:"[^"]*"'
    r',sessionID:"[^"]*"'
    r",enrichment:(null|\$R\[\d+\]=\{[^}]+\}|\{[^}]+\})"  # G11: Go marker
    r"\}"
)
_USAGE_COST_SCALE: float = 1e-8  # raw cost int → USD

# OpenCode _server function ID used for workspace discovery.
# This is a stable server-side identifier for the workspaces endpoint.
_SERVER_FN_ID = "def39973159c7f0483d8793a822b8dbb10d067e12c65455fcb4608459ba0234f"


class OpenCodeCollector(BaseCollector):
    PROVIDER_ID = "opencode"
    DEFAULT_WINDOW_TYPE = "weekly"

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        "web": ("Web API (Browser Cookie)", "_get_opencode_web"),
        "sidecar": ("Sidecar Aggregation (Multi-Host)", "_strategy_sidecar_aggregation"),
        "local": ("Local Database", "_strategy_local_db_fallback", {"enrich": True}),
    }

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)
        self._last_window_info: dict[str, dict] | None = self._load_persisted_state()

    def _state_file_path(self) -> str:
        """Path to the local state file for OpenCode."""
        acc_id = self.account_id or "default"
        return os.path.join(settings.data_dir, f"opencode_state_{acc_id}.json")

    def _load_persisted_state(self) -> dict[str, dict] | None:
        """Load window_info from local JSON state file."""
        path = self._state_file_path()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                    # Convert ISO timestamps back to datetime objects
                    for win in data.values():
                        if win.get("cutoff"):
                            win["cutoff"] = datetime.fromisoformat(win["cutoff"])
                        if win.get("reset_at"):
                            win["reset_at"] = datetime.fromisoformat(win["reset_at"])
                    return data
            except Exception as e:
                logger.debug(f"Failed to load OpenCode state from {path}: {e}")
        return None

    def _save_persisted_state(self, window_info: dict[str, dict]) -> None:
        """Save window_info to local JSON state file."""
        path = self._state_file_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Convert datetime objects to ISO strings for JSON
            serializable = {}
            for key, info in window_info.items():
                copy = info.copy()
                if isinstance(copy.get("cutoff"), datetime):
                    copy["cutoff"] = copy["cutoff"].isoformat()
                if isinstance(copy.get("reset_at"), datetime):
                    copy["reset_at"] = copy["reset_at"].isoformat()
                serializable[key] = copy

            with open(path, "w") as f:
                json.dump(serializable, f)
        except Exception as e:
            logger.debug(f"Failed to save OpenCode state to {path}: {e}")

    async def is_configured(self) -> bool:
        """Check if OpenCode session cookie or local DB is present."""
        # Check for session cookie (browser or manually entered via UI)
        session_cookie = await asyncio.to_thread(get_opencode_session_cookie)
        if not session_cookie:
            session_cookie = await token_cache.get_token(
                "opencode", "cookie_session", account_id=self.account_id or "default"
            )
        if session_cookie:
            return True

        # Check for local DB if enabled
        if is_local_collector_enabled():
            potential_paths = [
                settings.OPENCODE_DB_PATH,
                os.path.expanduser("~/.local/share/opencode/opencode.db"),
                os.path.expanduser("~/.opencode/opencode.db"),
            ]
            if any(os.path.exists(p) for p in potential_paths):
                return True

        return False

    def _fallback_strategies(self) -> list[Any]:
        """Return the fallback strategies for OpenCode (Sidecar, Local DB)."""
        return [
            self._strategy_sidecar_aggregation,
            self._strategy_local_db_fallback,
        ]

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """OpenCode Web API strategy."""
        return await self._get_opencode_web(client)

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return empty list on failure (OpenCode is non-critical)."""
        return []

    async def _strategy_sidecar_aggregation(
        self, client: httpx.AsyncClient
    ) -> list[dict[str, Any]]:
        """Second tier: Sidecar aggregation of multi-host data."""
        return await external_metric_service.get_opencode_aggregated()

    async def _strategy_local_db_fallback(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Third tier: Local database collection (if enabled)."""
        if is_local_collector_enabled():
            return await self._get_opencode_tui()
        return []

    async def _get_opencode_web(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """
        Fetch OpenCode usage from web API using Chrome cookies.

        This queries the opencode.ai servers and returns aggregated usage
        from ALL devices where the user is logged in (web IDE, TUI, etc.).

        Process:
        1. Extract session cookie from Chrome
        2. Call workspaces endpoint to get workspace ID
        3. Call subscription endpoint to get usage data
        4. Parse JavaScript response with regex

        Returns:
            List[Dict[str, Any]]: Cards for 5h and weekly windows, or empty list on failure
        """
        # Check for session cookie (browser, then token cache for UI-entered values)
        session_cookie = await asyncio.to_thread(get_opencode_session_cookie)
        input_source = "server" if session_cookie else None

        if not session_cookie:
            session_cookie = await token_cache.get_token(
                "opencode", "cookie_session", account_id=self.account_id or "default"
            )
            if session_cookie:
                input_source = "config"

        if not session_cookie:
            session_cookie = await token_cache.get_token(
                "opencode", "session_cookie", account_id=self.account_id or "default"
            )
            if session_cookie:
                input_source = "config"

        if not session_cookie:
            return []

        try:
            headers = {
                "Cookie": f"auth={session_cookie}",
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "Referer": "https://opencode.ai/",
                "Origin": "https://opencode.ai",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }

            # 1. Get workspace ID
            workspace_id = await self._get_workspace_id(client, headers)
            if not workspace_id:
                return []

            # 2. Get subscription data (rolling-window percentages from /go)
            usage_data = await self._get_subscription_data(client, headers, workspace_id)
            if not usage_data:
                return []

            # 3. Extract window info from usage_data first (for breakdown filtering)
            window_info = self._extract_window_info(usage_data)
            self._last_window_info = window_info
            await asyncio.to_thread(self._save_persisted_state, window_info)

            # Log window detection for debugging
            for win_key, info in window_info.items():
                logger.info(f"OpenCode: {win_key} window: is_fixed={info.get('is_fixed')}")

            # 4. Fetch per-model usage records from /usage (best-effort enrichment)
            breakdown: dict[str, Any] | None = None
            try:
                usage_page = await self._get_usage_page(client, headers, workspace_id)
                if usage_page:
                    records = self._parse_usage_records(usage_page)
                    if records:
                        breakdown = self._build_usage_breakdown(
                            records, datetime.now(UTC), window_info
                        )
            except Exception as e:
                logger.warning(f"OpenCode: /usage enrichment failed: {e}")

            # 5. Parse and return cards (enriched when breakdown is available)
            return self._parse_usage_data(
                usage_data, workspace_id, breakdown, input_source or "server", window_info
            )

        except Exception as e:
            logger.warning(f"OpenCode: Web API collection failed: {e}")
            return []

    async def _get_workspace_id(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> str | None:
        """Get the first workspace ID from opencode.ai."""
        try:
            # Check for env override first
            env_workspace = os.getenv("OPENCODE_WORKSPACE_ID")
            if env_workspace:
                # Handle full URL format
                if "workspace/" in env_workspace:
                    return env_workspace.split("workspace/")[-1].split("/")[0]
                return env_workspace

            ws_headers = headers.copy()
            ws_headers.update(
                {
                    "X-Server-Id": _SERVER_FN_ID,
                    "X-Server-Instance": f"server-fn:{uuid.uuid4()}",
                    "Accept": "text/javascript, application/json;q=0.9, */*;q=0.8",
                }
            )

            # Try primary GET approach
            url = f"https://opencode.ai/_server?id={_SERVER_FN_ID}"
            resp = await http_request_with_retry(
                client, "GET", url, headers=ws_headers, timeout=10.0, follow_redirects=True
            )

            # Fallback to POST with empty body if GET fails
            if resp.status_code != 200:
                resp = await http_request_with_retry(
                    client,
                    "POST",
                    "https://opencode.ai/_server",
                    headers=ws_headers,
                    json=[],
                    timeout=10.0,
                    follow_redirects=True,
                )

            if resp.status_code != 200:
                return None

            # Parse JavaScript response
            text = resp.text

            # Try to capture email here too
            email_match = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", text)
            if email_match:
                self.account_label = email_match.group(1)

            # Look for workspace ID pattern: id:"wrk_..."
            match = re.search(r'id:"(wrk_[a-zA-Z0-9]+)"', text)
            if match:
                return match.group(1)

            return None
        except Exception as e:
            logger.warning(f"OpenCode: Workspace discovery failed: {e}")
            return None

    async def _get_subscription_data(
        self, client: httpx.AsyncClient, headers: dict[str, str], workspace_id: str
    ) -> str | None:
        """Get subscription/usage data from the workspace page (GET)."""
        try:
            url = f"https://opencode.ai/workspace/{workspace_id}/go"
            # Switch to HTML accept header for the page fetch
            usage_headers = headers.copy()
            usage_headers["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            )

            resp = await http_request_with_retry(
                client, "GET", url, headers=usage_headers, timeout=15.0, follow_redirects=True
            )

            if resp.status_code != 200:
                return None

            return resp.text
        except Exception as e:
            logger.warning(f"OpenCode: Subscription data fetch failed: {e}")
            return None

    async def _get_usage_page(
        self, client: httpx.AsyncClient, headers: dict[str, str], workspace_id: str
    ) -> str | None:
        """Fetch the /usage page which embeds per-model usage records as inline JS."""
        try:
            url = f"https://opencode.ai/workspace/{workspace_id}/usage"
            page_headers = headers.copy()
            page_headers["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            )
            resp = await http_request_with_retry(
                client, "GET", url, headers=page_headers, timeout=15.0, follow_redirects=True
            )

            if resp.status_code == 200:
                record_count = resp.text.count('id:"usg_')
                logger.info(
                    f"OpenCode: Fetched /usage page, length: {len(resp.text)}, records: {record_count}"
                )
            else:
                logger.warning(f"OpenCode: /usage fetch failed, status: {resp.status_code}")

            return resp.text if resp.status_code == 200 else None
        except Exception as e:
            logger.warning(f"OpenCode: /usage fetch exception: {e}")
            return None

    def _parse_usage_records(self, text: str) -> list[dict[str, Any]]:
        """
        Extract per-call usage records from the inline JS on the /usage page.

        Each record has: ts (datetime), model_short (str), source ("go"|"free"|"api"),
        input/output/reasoning/cache_read (int), cost_usd (float).

        Classification rules:
          enrichment != "null"         → go   (subscription, charged against Go quota)
          enrichment == "null", cost=0 → free (free-tier model)
          enrichment == "null", cost>0 → api  (own API key, pay-as-you-go)
        """
        records = []
        for m in _USAGE_RECORD_RE.finditer(text):
            (
                ts_str,
                model,
                _provider,
                t_in,
                t_out,
                t_reason,
                cache_r,
                _cw5,
                _cw1,
                cost_raw,
                enrichment,
            ) = m.groups()
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            cost_int = int(cost_raw)
            if enrichment != "null":
                source = "go"
            elif cost_int == 0:
                source = "free"
            else:
                source = "api"

            records.append(
                {
                    "ts": ts,
                    "model": model,
                    "model_short": self._short_model_id_oc(model),
                    "source": source,
                    "input": max(0, int(t_in)),
                    "output": max(0, int(t_out)),
                    "reasoning": 0 if t_reason == "null" else max(0, int(t_reason)),
                    "cache_read": max(0, int(cache_r)),
                    "cost_usd": max(0, cost_int) * _USAGE_COST_SCALE,
                }
            )

        if records:
            logger.info(f"OpenCode: Parsed {len(records)} usage records")
        else:
            sample = text[:200] if text else "(empty)"
            logger.debug(f"OpenCode: No usage records parsed (enrichment-only), sample: {sample}")

        return records

    def _build_usage_breakdown(
        self,
        records: list[dict[str, Any]],
        now: datetime,
        window_info: dict[str, dict] | None = None,
    ) -> dict[str, Any]:
        """
        Aggregate records into per-source / per-window totals.

        Args:
            records: List of parsed usage records
            now: Current time
            window_info: Dict keyed by window_name (e.g., "rollingUsage")
                       with cutoff timestamps and is_fixed bool.
                       If None/empty, falls back to rolling windows for backward compatibility.

        Returns:
            {
              "go":  {"5h": {cost, msgs, tokens, by_model}, "7d": ..., "30d": ...},
              "free": {"lifetime": {cost, msgs, tokens, by_model}},
              "api":  {"lifetime": {cost, msgs, tokens, by_model}},
            }
        """

        def _empty_bucket() -> dict[str, Any]:
            return {
                "cost": 0.0,
                "msgs": 0,
                "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0},
                "by_model": {},
            }

        def _add_to_bucket(bucket: dict, r: dict) -> None:
            bucket["cost"] += r["cost_usd"]
            bucket["msgs"] += 1
            for k in ("input", "output", "reasoning", "cache_read"):
                bucket["tokens"][k] += r.get(k, 0)
            entry = bucket["by_model"].setdefault(r["model_short"], {"cost": 0.0, "msgs": 0})
            entry["cost"] += r["cost_usd"]
            entry["msgs"] += 1

        # Map window keys to internal window names
        window_map = {
            "rollingUsage": "5h",
            "weeklyUsage": "7d",
            "monthlyUsage": "30d",
        }

        # If no window_info provided, fall back to rolling windows (backward compat)
        use_fallback = not window_info

        result: dict[str, Any] = {
            "go": {w: _empty_bucket() for w in ["5h", "7d", "30d"]},
            "free": {"lifetime": _empty_bucket()},
            "api": {"lifetime": _empty_bucket()},
        }

        for r in records:
            source = r["source"]
            if source == "go":
                # Find which windows this record belongs to
                for win_key, internal_name in window_map.items():
                    if use_fallback:
                        # Fallback: use simple rolling windows
                        if internal_name == "5h":
                            cutoff = now - timedelta(hours=5)
                        elif internal_name == "7d":
                            cutoff = now - timedelta(days=7)
                        else:
                            cutoff = now - timedelta(days=30)
                    elif win_key in window_info:
                        cutoff = window_info[win_key]["cutoff"]
                    else:
                        continue

                    if r["ts"] >= cutoff:
                        _add_to_bucket(result["go"][internal_name], r)
            elif source == "free":
                _add_to_bucket(result["free"]["lifetime"], r)
            else:
                _add_to_bucket(result["api"]["lifetime"], r)

        return result

    def _build_free_api_card(
        self,
        source: str,
        data: dict[str, Any],
        workspace_id: str,
        email: str,
        now_iso: str,
        input_source: str = "server",
    ) -> dict[str, Any]:
        """Build a card for Free-tier or API (pay-as-you-go) usage."""
        usage_url = f"https://opencode.ai/workspace/{workspace_id}/usage"
        identity_suffix = f" | {email}" if email else ""

        totals = {
            "cost": data["cost"],
            "msgs": data["msgs"],
            "tokens": data["tokens"],
            "by_model": data["by_model"],
            "convos": 0,
        }
        detail = self._build_oc_enrichment_detail(totals)

        if source == "free":
            t_in = data["tokens"]["input"]
            t_out = data["tokens"]["output"]
            total_tokens = t_in + t_out
            # Format token count for the primary display (e.g. "1,234,567 tokens")
            tok_display = f"{total_tokens:,} tokens"
            detail += f" · Free tier{identity_suffix}"
            return {
                "service_name": "OpenCode",
                "variant": "Free",
                "window_type": "rolling",
                "icon": "⚡",
                "remaining": tok_display,
                "unit": "free tier",
                "reset": "Lifetime",
                "health": "good",
                "pace": "—",
                "detail": detail,
                "used_value": total_tokens,
                "limit_value": None,
                "is_unlimited": True,
                "unit_type": "token",
                "currency": "USD",
                "account_label": email,
                "reset_at": None,
                "tier": "Free",
                "provider_id": "opencode",
                "data_source": self.DATA_SOURCE_WEB,
                "input_source": input_source,
                "usage_url": usage_url,
                "updated_at": now_iso,
            }
        # api
        detail += f" · API key{identity_suffix}"
        return {
            "service_name": "OpenCode",
            "variant": "API",
            "window_type": "rolling",
            "icon": "⚡",
            # remaining shows total spend; detail falls through to subtitle
            "remaining": f"${data['cost']:.4f}",
            "unit": "pay-as-you-go",
            "reset": "Lifetime",
            "health": "good",
            "pace": "—",
            "detail": detail,
            "used_value": data["cost"],
            "limit_value": None,
            "is_unlimited": False,
            "unit_type": "currency",
            "currency": "USD",
            "account_label": email,
            "reset_at": None,
            "tier": "API",
            "provider_id": "opencode",
            "data_source": self.DATA_SOURCE_WEB,
            "input_source": input_source,
            "usage_url": usage_url,
            "updated_at": now_iso,
        }
        # api
        detail += f" · API key{identity_suffix}"
        return {
            "service_name": "OpenCode",
            "variant": "API",
            "window_type": "rolling",
            "icon": "⚡",
            # remaining shows total spend; detail falls through to subtitle
            "remaining": f"${data['cost']:.4f}",
            "unit": "pay-as-you-go",
            "reset": "Lifetime",
            "health": "good",
            "pace": "—",
            "detail": detail,
            "used_value": data["cost"],
            "limit_value": None,
            "is_unlimited": False,
            "unit_type": "currency",
            "currency": "USD",
            "account_label": email,
            "reset_at": None,
            "tier": "API",
            "data_source": self.DATA_SOURCE_WEB,
            "input_source": input_source,
            "usage_url": usage_url,
            "updated_at": now_iso,
        }

    def _parse_usage_data(
        self,
        text: str,
        workspace_id: str,
        breakdown: dict[str, Any] | None = None,
        input_source: str = "server",
        window_info: dict[str, dict] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Parse JavaScript/React stream response to extract usage data.
        """
        # logger.info(f"OpenCode parsing usage data (text length: {len(text)})")

        cards = []
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        usage_url = f"https://opencode.ai/workspace/{workspace_id}/go"

        # Discover email for account_label
        email = ""
        email_match = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", text)
        if email_match:
            email = email_match.group(1)
            self.account_label = email

        identity_suffix = f" | {email}" if email else ""

        # Definition of windows to search for
        # Each tuple: (text-key, service_name, limit_usd, window_type, reset_label_for_display)
        # window_type is the canonical enum used for card identity; reset_label is the human "in N days" hint.
        windows = [
            ("rollingUsage", "OpenCode", 12.0, "session", "5h"),
            ("weeklyUsage", "OpenCode", 30.0, "weekly", "7d"),
            ("monthlyUsage", "OpenCode", 60.0, "monthly", "30d"),
        ]

        for key, service_name, limit, window_type, reset_label in windows:
            # Even more flexible regex
            # key:($R[xx]=)?{...}
            pattern = rf"{key}:(?:\$R\[\d+\]=)?\{{([^}}]+)\}}"
            match = re.search(pattern, text)

            if not match:
                logger.info(f"OpenCode: Could not find object for {key}")
                continue

            obj_content = match.group(1)
            # logger.info(f"OpenCode: Found {key} object content: {obj_content}")

            # Extract fields from the object content
            pct_match = re.search(r"usagePercent:([\d.]+)", obj_content)
            reset_match = re.search(r"resetInSec:(\d+)", obj_content)

            if not pct_match or not reset_match:
                logger.info(f"OpenCode: Missing fields in {key} object")
                continue

            pct = float(pct_match.group(1))
            reset_sec = int(reset_match.group(1))

            used = (pct / 100) * limit
            remaining = max(0, limit - used)
            reset_at = now + timedelta(seconds=reset_sec)

            cards.append(
                {
                    "service_name": service_name,
                    "icon": "⚡",
                    "remaining": f"${remaining:.2f}",
                    "unit": f"${limit:.0f} limit",
                    "reset": reset_label,
                    "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                    "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                    "detail": f"${used:.2f} used ({pct:.1f}%) · Web API{identity_suffix}",
                    "used_value": used,
                    "limit_value": limit,
                    "is_unlimited": False,
                    "unit_type": "currency",
                    "currency": "USD",
                    "account_label": email,
                    "reset_at": reset_at.isoformat(),
                    "window_type": window_type,
                    "provider_id": "opencode-go",
                    "tier": "Go",
                    "data_source": self.DATA_SOURCE_WEB,
                    "input_source": input_source,
                    "usage_url": usage_url,
                    "updated_at": now_iso,
                }
            )

        # Enrich Go cards with per-model token breakdown from the /usage page.
        # Cards carry canonical window_type ("session"/"weekly"/"monthly"); the upstream
        # API breakdown is keyed by the short codes "5h"/"7d"/"30d".
        breakdown_key_for = {"session": "5h", "weekly": "7d", "monthly": "30d"}
        if breakdown:
            logger.info(f"OpenCode: breakdown keys: {breakdown.keys()}")
            logger.info(f"OpenCode: breakdown[go] keys: {breakdown.get('go', {}).keys()}")
            for card in cards:
                wt = card.get("window_type")
                wk = breakdown_key_for.get(wt)
                logger.info(
                    f"OpenCode: card {card.get('service_name')} ({wt}) -> breakdown key: {wk}"
                )
                if not wk:
                    continue
                go_data = breakdown.get("go", {}).get(wk)
                logger.info(f"OpenCode: go_data for {wk}: {go_data}")
                if not go_data or go_data["msgs"] == 0:
                    logger.info(f"OpenCode: Skipping {wk} - no data")
                    continue

                # Build token_usage dict
                tokens = go_data["tokens"]
                token_usage = {
                    "input": tokens.get("input", 0),
                    "output": tokens.get("output", 0),
                    "reasoning": tokens.get("reasoning", 0),
                    "cache_read": tokens.get("cache_read", 0),
                }
                # Calculate total
                token_usage["total"] = (
                    token_usage["input"] + token_usage["output"] + token_usage["reasoning"]
                )

                # Add structured token fields to card
                card["token_usage"] = token_usage
                card["by_model"] = go_data.get("by_model", {})
                card["msgs"] = go_data["msgs"]
                card["pct_used"] = (
                    (card.get("used_value", 0) / card.get("limit_value", 1)) * 100
                    if card.get("limit_value")
                    else 0
                )
                logger.info(
                    f"OpenCode: Added token fields to {card.get('service_name')}: token={token_usage.get('total')}"
                )

                # Also update detail string for display
                suffix = self._build_oc_enrichment_detail(
                    {
                        "cost": go_data["cost"],
                        "msgs": go_data["msgs"],
                        "tokens": go_data["tokens"],
                        "by_model": go_data["by_model"],
                        "convos": 0,
                    }
                )
                if suffix:
                    existing = card.get("detail", "").rstrip()
                    card["detail"] = f"{existing} | {suffix}".strip(" |")

            # Emit Free card if there is any free-tier usage
            free_data = breakdown.get("free", {}).get("lifetime", {})
            if free_data.get("msgs", 0) > 0:
                cards.append(
                    self._build_free_api_card(
                        "free", free_data, workspace_id, email, now_iso, input_source
                    )
                )

            # Emit API card if there is any pay-as-you-go usage
            api_data = breakdown.get("api", {}).get("lifetime", {})
            if api_data.get("msgs", 0) > 0:
                cards.append(
                    self._build_free_api_card(
                        "api", api_data, workspace_id, email, now_iso, input_source
                    )
                )

        # logger.info(f"OpenCode: _parse_usage_data returning {len(cards)} cards")
        return cards

    async def _get_opencode_tui(self) -> list[dict[str, Any]]:
        """
        Query local DB and emit one enrichment dict per window.

        Returns enrichment-shaped dicts (not real cards) carrying
        `_enrichment_detail`, `totals`, and `_fallback_card`. The custom
        `_enrich_results` merges them into the web-API primary cards; when
        there is no primary, `_fallback_card` is promoted so local-only
        hosts still render a card.
        """
        potential_paths = [
            settings.OPENCODE_DB_PATH,
            os.path.expanduser("~/.local/share/opencode/opencode.db"),
            os.path.expanduser("~/.opencode/opencode.db"),
        ]

        db = None
        for p in potential_paths:
            if os.path.exists(p):
                db = p
                logger.debug(f"Found OpenCode database at: {p}")
                break

        if not db:
            logger.debug("No OpenCode database found in any of the potential paths.")
            return []

        try:
            import aiosqlite

            now = datetime.now(UTC)

            async with aiosqlite.connect(db) as conn:
                # Discover identity if not already set via UI/Web
                if not self.account_label or self.account_label.lower() == "default":
                    # 1. Check for environment variable override
                    env_label = os.getenv("OPENCODE_ACCOUNT_LABEL")
                    if env_label:
                        self.account_label = env_label
                    else:
                        # 2. Check local account table
                        try:
                            cursor = await conn.execute("SELECT email FROM account LIMIT 1")
                            row = await cursor.fetchone()
                            if row and row[0]:
                                self.account_label = row[0]
                            else:
                                self.account_label = "Default"
                        except Exception:
                            self.account_label = "Default"

                identity_suffix = f" | {self.account_label}" if self.account_label else ""

                # Define windows. If we have web-discovered info, use those exact cutoffs.
                # Mapping: rollingUsage->session, weeklyUsage->weekly, monthlyUsage->monthly
                win_map = {
                    "session": ("rollingUsage", 5, 12.0),
                    "weekly": ("weeklyUsage", 7 * 24, 30.0),
                    "monthly": ("monthlyUsage", 30 * 24, 60.0),
                }

                windows = []
                for window_type, (web_key, rolling_hours, limit) in win_map.items():
                    # Check if we have a fresh cutoff from the web API
                    web_info = (self._last_window_info or {}).get(web_key)
                    if web_info and "cutoff" in web_info:
                        cutoff_ms = int(web_info["cutoff"].timestamp() * 1000)
                    else:
                        # Fallback to rolling
                        cutoff_ms = int((now - timedelta(hours=rolling_hours)).timestamp() * 1000)

                    windows.append((web_key, cutoff_ms, "OpenCode", limit, window_type))

                results = []
                for _window_key, cutoff_ms, service_name, limit, window_type in windows:
                    cursor = await conn.execute(
                        """
                        SELECT
                            json_extract(data, '$.cost'),
                            json_extract(data, '$.tokens.input'),
                            json_extract(data, '$.tokens.output'),
                            json_extract(data, '$.tokens.reasoning'),
                            json_extract(data, '$.tokens.cache.read'),
                            json_extract(data, '$.tokens.cache.write'),
                            json_extract(data, '$.modelID'),
                            json_extract(data, '$.parentID')
                        FROM message
                        WHERE time_created > ?
                          AND json_extract(data, '$.role') = 'assistant'
                          AND json_extract(data, '$.providerID') = 'opencode-go'
                        """,
                        (cutoff_ms,),
                    )
                    rows = await cursor.fetchall()

                    total_cost = 0.0
                    total_in = total_out = total_reason = cache_r = cache_w = 0
                    by_model: dict[str, dict] = {}
                    convos: set[str] = set()

                    for cost, t_in, t_out, t_reason, cr, cw, model_id, parent_id in rows:
                        total_cost += float(cost or 0)
                        total_in += int(t_in or 0)
                        total_out += int(t_out or 0)
                        total_reason += int(t_reason or 0)
                        cache_r += int(cr or 0)
                        cache_w += int(cw or 0)
                        if parent_id:
                            convos.add(parent_id)
                        if model_id:
                            short = self._short_model_id_oc(model_id)
                            entry = by_model.setdefault(short, {"cost": 0.0, "msgs": 0})
                            entry["cost"] += float(cost or 0)
                            entry["msgs"] += 1

                    msgs = len(rows)
                    remaining = max(0.0, limit - total_cost)
                    pct = (total_cost / limit * 100) if limit > 0 else 0.0

                    totals = {
                        "cost": total_cost,
                        "msgs": msgs,
                        "convos": len(convos),
                        "tokens": {
                            "input": total_in,
                            "output": total_out,
                            "reasoning": total_reason,
                            "cache_read": cache_r,
                            "cache_write": cache_w,
                        },
                        "by_model": by_model,
                    }

                    enrichment_detail = self._build_oc_enrichment_detail(totals)

                    # Map totals to canonical card fields
                    token_usage = totals["tokens"].copy()
                    token_usage["total"] = (
                        token_usage["input"] + token_usage["output"] + token_usage["reasoning"]
                    )

                    fallback_card = {
                        "service_name": service_name,
                        "window_type": window_type,
                        "provider_id": "opencode-go",
                        "icon": "⚡",
                        "remaining": f"${remaining:.2f}",
                        "unit": f"${limit:.0f} limit",
                        "reset": "Rolling" if window_type == "session" else "n/a",
                        "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                        "pace": "Stable" if pct < 50 else "High" if pct < 80 else "Fatigue",
                        "detail": f"${total_cost:.2f} used · {msgs} msgs · Local DB{identity_suffix}",
                        "used_value": total_cost,
                        "limit_value": limit,
                        "is_unlimited": False,
                        "unit_type": "currency",
                        "currency": "USD",
                        "account_label": self.account_label,
                        "reset_at": None,
                        "tier": "Go",
                        "data_source": self.DATA_SOURCE_LOCAL,
                        "input_source": "server",
                        "updated_at": datetime.now(UTC).isoformat(),
                        "token_usage": token_usage,
                        "by_model": by_model,
                        "msgs": msgs,
                        "pct_used": pct,
                    }

                    results.append(
                        {
                            "service_name": service_name,
                            "window_type": window_type,
                            "_enrichment_detail": enrichment_detail,
                            "totals": totals,
                            "_fallback_card": fallback_card,
                        }
                    )

                free_cursor = await conn.execute(
                    """
                    SELECT
                        json_extract(data, '$.tokens.input'),
                        json_extract(data, '$.tokens.output'),
                        json_extract(data, '$.tokens.reasoning'),
                        json_extract(data, '$.tokens.cache.read'),
                        json_extract(data, '$.tokens.cache.write'),
                        json_extract(data, '$.modelID'),
                        json_extract(data, '$.parentID')
                    FROM message
                    WHERE json_extract(data, '$.role') = 'assistant'
                      AND json_extract(data, '$.providerID') = 'opencode'
                    """,
                )
                free_rows = await free_cursor.fetchall()

                session_cutoff_ms = int((now - timedelta(hours=5)).timestamp() * 1000)
                session_cursor = await conn.execute(
                    """
                    SELECT
                        json_extract(data, '$.tokens.input'),
                        json_extract(data, '$.tokens.output'),
                        json_extract(data, '$.tokens.reasoning'),
                        json_extract(data, '$.tokens.cache.read'),
                        json_extract(data, '$.tokens.cache.write'),
                        json_extract(data, '$.modelID'),
                        json_extract(data, '$.parentID')
                    FROM message
                    WHERE time_created > ?
                      AND json_extract(data, '$.role') = 'assistant'
                      AND json_extract(data, '$.providerID') = 'opencode'
                    """,
                    (session_cutoff_ms,),
                )
                session_rows = await session_cursor.fetchall()

                def sum_free_tokens(rows: list) -> tuple[int, int, int, int, int, dict, int]:
                    total_in = total_out = total_reason = cache_r = cache_w = 0
                    by_model: dict[str, dict] = {}
                    convos: set[str] = set()
                    for t_in, t_out, t_reason, cr, cw, model_id, parent_id in rows:
                        total_in += int(t_in or 0)
                        total_out += int(t_out or 0)
                        total_reason += int(t_reason or 0)
                        cache_r += int(cr or 0)
                        cache_w += int(cw or 0)
                        if parent_id:
                            convos.add(parent_id)
                        if model_id:
                            short = self._short_model_id_oc(model_id)
                            entry = by_model.setdefault(short, {"cost": 0.0, "msgs": 0})
                            entry["msgs"] += 1
                    msgs = len(rows)
                    return total_in, total_out, total_reason, cache_r, cache_w, by_model, msgs

                lt_in, lt_out, lt_reason, lt_cache_r, lt_cache_w, lt_by_model, lt_msgs = (
                    sum_free_tokens(free_rows)
                )
                se_in, se_out, se_reason, se_cache_r, se_cache_w, se_by_model, se_msgs = (
                    sum_free_tokens(session_rows)
                )

                if lt_msgs > 0:
                    lifetime_tokens = lt_in + lt_out + lt_reason
                    session_tokens = se_in + se_out + se_reason

                    free_totals = {
                        "lifetime": {
                            "tokens": {
                                "input": lt_in,
                                "output": lt_out,
                                "reasoning": lt_reason,
                                "cache_read": lt_cache_r,
                                "cache_write": lt_cache_w,
                            },
                            "msgs": lt_msgs,
                            "by_model": lt_by_model,
                        },
                        "session": {
                            "tokens": {
                                "input": se_in,
                                "output": se_out,
                                "reasoning": se_reason,
                                "cache_read": se_cache_r,
                                "cache_write": se_cache_w,
                            },
                            "msgs": se_msgs,
                            "by_model": se_by_model,
                        },
                    }

                    free_token_usage = {
                        "input": se_in,
                        "output": se_out,
                        "reasoning": se_reason,
                        "cache_read": se_cache_r,
                        "cache_write": se_cache_w,
                        "total": session_tokens,
                    }

                    session_tok = free_totals["session"]["tokens"]
                    free_detail_parts = []
                    if session_tok.get("input"):
                        free_detail_parts.append(f"in:{session_tok['input']:,}")
                    if session_tok.get("output"):
                        free_detail_parts.append(f"out:{session_tok['output']:,}")
                    if session_tok.get("cache_read"):
                        free_detail_parts.append(f"cache_r:{session_tok['cache_read']:,}")
                    free_detail = " ".join(free_detail_parts) if free_detail_parts else "$0.00"
                    free_detail += f" · Lifetime: {lifetime_tokens:,} tokens{identity_suffix}"

                    free_fallback_card = {
                        "service_name": service_name,
                        "window_type": "rolling",
                        "variant": "Free",
                        "provider_id": "opencode",
                        "icon": "⚡",
                        "remaining": f"{lifetime_tokens:,} tokens",
                        "unit": "free tier",
                        "reset": "Lifetime",
                        "health": "good",
                        "pace": "—",
                        "detail": f"{session_tokens:,} tokens in last 5h · {se_msgs} msgs | {free_detail}",
                        "used_value": lifetime_tokens,
                        "limit_value": None,
                        "is_unlimited": True,
                        "unit_type": "token",
                        "currency": "USD",
                        "account_label": self.account_label,
                        "reset_at": None,
                        "tier": "Free",
                        "data_source": self.DATA_SOURCE_LOCAL,
                        "input_source": "server",
                        "updated_at": datetime.now(UTC).isoformat(),
                        "token_usage": free_token_usage,
                        "by_model": se_by_model,
                        "msgs": se_msgs,
                        "pct_used": None,
                    }

                    results.append(
                        {
                            "service_name": service_name,
                            "window_type": "rolling",
                            "variant": "Free",
                            "_enrichment_detail": free_detail,
                            "totals": free_totals,
                            "_fallback_card": free_fallback_card,
                        }
                    )

            return results

        except Exception as e:
            return [
                error_card(
                    "OpenCode TUI",
                    "⚡",
                    f"DB Error: {str(e)[:15]}",
                    error_type="api_error",
                )
            ]

    def _short_model_id_oc(self, model_id: str) -> str:
        """Shorten a model ID for display, e.g. claude-sonnet-4-6 → sonnet."""
        m = model_id.lower()
        # Strip claude- prefix then any trailing -version suffix
        m = re.sub(r"^claude-", "", m)
        m = re.sub(r"-\d+[-.]?\d*$", "", m)
        # Trim -free / -latest suffixes
        m = re.sub(r"-(free|latest|preview)$", "", m)
        return m or model_id

    def _extract_window_info(self, text: str) -> dict[str, dict]:
        """
        Extract window type (rolling vs fixed) from the usage text.

        Returns dict keyed by window name (e.g., "rollingUsage")
        with cutoff timestamp and is_fixed flag.
        """
        now = datetime.now(UTC)
        windows = [
            ("rollingUsage", "5h"),
            ("weeklyUsage", "7d"),
            ("monthlyUsage", "30d"),
        ]

        FIXED_RESET_THRESHOLD = 86400  # 1 day in seconds
        result: dict[str, dict] = {}

        for key, duration_label in windows:
            pattern = rf"{key}:(?:\$R\[\d+\]=)?\{{([^}}]+)\}}"
            match = re.search(pattern, text)
            if not match:
                continue

            obj_content = match.group(1)
            reset_match = re.search(r"resetInSec:(\d+)", obj_content)
            if not reset_match:
                continue

            reset_sec = int(reset_match.group(1))
            is_fixed = reset_sec > FIXED_RESET_THRESHOLD

            if is_fixed:
                reset_at = now + timedelta(seconds=reset_sec)
                if key == "weeklyUsage":
                    cutoff = reset_at - timedelta(days=7)
                elif key == "monthlyUsage":
                    cutoff = reset_at - timedelta(days=30)
                else:
                    cutoff = reset_at - timedelta(hours=5)
            elif key == "rollingUsage":
                cutoff = now - timedelta(hours=5)
            elif key == "weeklyUsage":
                cutoff = now - timedelta(days=7)
            else:
                cutoff = now - timedelta(days=30)

            result[key] = {
                "cutoff": cutoff,
                "is_fixed": is_fixed,
            }

        return result

    def _build_oc_enrichment_detail(self, totals: dict) -> str:
        """Build the enrichment detail string from per-window totals."""
        parts: list[str] = []

        cost = totals.get("cost", 0.0)
        parts.append(f"${cost:.2f}")

        tok = totals.get("tokens", {})
        token_segs: list[str] = []
        if tok.get("input"):
            token_segs.append(f"in:{tok['input']:,}")
        if tok.get("output"):
            token_segs.append(f"out:{tok['output']:,}")
        if tok.get("cache_read"):
            token_segs.append(f"cache_r:{tok['cache_read']:,}")
        if tok.get("cache_write"):
            token_segs.append(f"cache_w:{tok['cache_write']:,}")
        if token_segs:
            parts.append(" ".join(token_segs))

        by_model = totals.get("by_model", {})
        if by_model:
            top = sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True)[:3]
            model_segs = [f"{name}:${info['cost']:.2f}" for name, info in top]
            parts.append(" ".join(model_segs))

        convos = totals.get("convos", 0)
        if convos:
            parts.append(f"{convos} convos")

        return " | ".join(parts)

    def _enrich_results(
        self,
        primary: list[dict[str, Any]] | None,
        enrichment: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not enrichment or self._is_error_result(enrichment):
            return primary or []

        # Local-only host: promote fallback cards so the account still renders.
        if not primary or self._is_error_result(primary):
            promoted = [e["_fallback_card"] for e in enrichment if e.get("_fallback_card")]
            return promoted or (primary or [])

        # Check for unmatched variant-specific fallbacks (Free, API, etc.)
        # Web API may not return these if there's no usage, but local DB might have data
        for e in enrichment:
            fb = e.get("_fallback_card")
            if not fb:
                continue
            variant = fb.get("variant")
            if not variant:
                continue

            primary_variant_exists = any(c.get("variant") == variant for c in primary)

            if not primary_variant_exists:
                primary.append(fb)

        by_name = {
            (e.get("service_name"), e.get("window_type")): e
            for e in enrichment
            if e.get("_enrichment_detail")
        }
        for card in primary:
            match = by_name.get((card.get("service_name"), card.get("window_type")))
            if not match:
                continue

            # 1. Update detail suffix
            suffix = match["_enrichment_detail"]
            if suffix:
                card["detail"] = f"{card.get('detail', '').rstrip()} | {suffix}".strip(" |")

            # 2. Always prefer local token data - it's more complete/accurate than web API
            fb_card = match.get("_fallback_card", {})
            for field in ["token_usage", "by_model", "msgs"]:
                if field in fb_card:
                    card[field] = fb_card[field]

        return primary
