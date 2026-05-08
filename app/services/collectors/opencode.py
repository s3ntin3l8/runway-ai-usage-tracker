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

from app.core.config import settings
from app.core.utils import PaceCalculator, http_request_with_retry
from app.services.collectors.base import BaseCollector, format_token_details
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
        "web": ("Web API (session cookie)", "_get_opencode_web"),
        "sidecar": (
            "Sidecar Aggregation (Multi-Host)",
            "_strategy_sidecar_aggregation",
            {"enrich": True},
        ),
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
        """Check if OpenCode session cookie is available (sidecar-pushed or UI)."""
        session_cookie = await token_cache.get_token(
            "opencode", "cookie_session", account_id=self.account_id or "default"
        )
        return bool(session_cookie)

    def _fallback_strategies(self) -> list[Any]:
        """Return fallback strategies for OpenCode (sidecar aggregation only)."""
        return [
            self._strategy_sidecar_aggregation,
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
        """Second tier: Sidecar aggregation of multi-host data.

        Queries UsageEvent rows for provider_id='opencode' and aggregates cost
        across all sidecars into per-account Combined cards for the session (5h),
        weekly (7d), and monthly (30d) windows.
        """
        from sqlmodel import Session, select

        from app.core.db import engine
        from app.models.db import UsageEvent

        now = datetime.now(UTC)
        cutoffs = {
            "session": (now - timedelta(hours=5), 12.0),
            "weekly": (now - timedelta(days=7), 30.0),
            "monthly": (now - timedelta(days=30), 60.0),
        }

        # SQLite stores datetimes as naive strings; use naive UTC cutoff for the WHERE clause.
        cutoff_30d_naive = (now - timedelta(days=30)).replace(tzinfo=None)
        try:
            with Session(engine) as session:
                events = session.exec(
                    select(UsageEvent).where(
                        UsageEvent.provider_id == "opencode",
                        UsageEvent.ts >= cutoff_30d_naive,
                    )
                ).all()
        except Exception as e:
            logger.warning(f"OpenCode sidecar aggregation DB query failed: {e}")
            return []

        if not events:
            return []

        # Aggregate cost and message count per (account_id, window_type)
        # Structure: {account_id: {window_type: {"cost": float, "msgs": int}}}
        agg: dict[str, dict[str, dict[str, Any]]] = {}
        for event in events:
            acc = event.account_id or "default"
            if acc not in agg:
                agg[acc] = {wt: {"cost": 0.0, "msgs": 0} for wt in cutoffs}
            # SQLite may return timezone-naive datetimes; treat them as UTC for comparison.
            event_ts = event.ts
            if event_ts.tzinfo is None:
                event_ts = event_ts.replace(tzinfo=UTC)
            for window_type, (cutoff, _limit) in cutoffs.items():
                if event_ts >= cutoff:
                    agg[acc][window_type]["cost"] += event.cost_usd
                    agg[acc][window_type]["msgs"] += 1

        cards: list[dict[str, Any]] = []
        now_iso = now.isoformat()
        for acc_id, windows in agg.items():
            for window_type, (cutoff, limit) in cutoffs.items():
                data = windows[window_type]
                if data["msgs"] == 0:
                    continue
                used = data["cost"]
                msgs = data["msgs"]
                remaining = max(0.0, limit - used)
                pct = (used / limit * 100) if limit > 0 else 0.0
                cards.append(
                    {
                        "provider_id": "opencode",
                        "account_id": acc_id,
                        "service_name": "OpenCode",
                        "variant": "Combined",
                        "window_type": window_type,
                        "icon": "⚡",
                        "remaining": f"${remaining:.2f}",
                        "unit": f"${limit:.0f} limit",
                        "reset": "Rolling",
                        "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                        "pace": "Stable" if pct < 50 else "High" if pct < 80 else "Fatigue",
                        "detail": f"${used:.2f} used · {msgs} msgs · Combined",
                        "used_value": used,
                        "limit_value": limit,
                        "pct_used": pct,
                        "msgs": msgs,
                        "unit_type": "currency",
                        "currency": "USD",
                        "data_source": "local",
                        "updated_at": now_iso,
                    }
                )

        return cards

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
        # Source: token cache — populated by sidecar push or via UI ProviderConfig
        session_cookie = None
        input_source = None
        res = await token_cache.get_with_metadata(
            "opencode", account_id=self.account_id or "default"
        )
        if res:
            tokens, metadata = res
            session_cookie = tokens.get("cookie_session") or tokens.get("session_cookie")
            if session_cookie:
                source = metadata.get("source")
                input_source = "sidecar" if source else "config"

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
            entry = bucket["by_model"].setdefault(
                r["model_short"],
                {
                    "cost": 0.0,
                    "msgs": 0,
                    "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0},
                },
            )
            entry["cost"] += r["cost_usd"]
            entry["msgs"] += 1
            for k in ("input", "output", "reasoning", "cache_read"):
                entry["tokens"][k] += r.get(k, 0)

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

        # Compute total tokens per model
        for source_buckets in result.values():
            for bucket in source_buckets.values():
                for entry in bucket["by_model"].values():
                    t = entry["tokens"]
                    t["total"] = t["input"] + t["output"] + t["reasoning"]

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
                # Enforce universal contract: output includes reasoning
                token_usage = {
                    "input": tokens.get("input", 0),
                    "output": tokens.get("output", 0) + tokens.get("reasoning", 0),
                    "reasoning": tokens.get("reasoning", 0),
                    "cache_read": tokens.get("cache_read", 0),
                }
                # Total is exactly input + output
                token_usage["total"] = token_usage["input"] + token_usage["output"]

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

        # Token summary
        tok = totals.get("tokens", {})
        tok_str = format_token_details(tok)
        if tok_str:
            parts.append(tok_str)

        by_model = totals.get("by_model", {})
        if by_model:
            top = sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True)[:3]
            model_segs = [f"{name}:${info['cost']:.2f}" for name, info in top]
            parts.append(" ".join(model_segs))

        convos = totals.get("convos", 0)
        if convos:
            parts.append(f"{convos} convos")

        return " | ".join(parts)
