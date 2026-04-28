"""
Anthropic (Claude) local data collection strategies.

Handles:
- CLI PTY fallback (running `claude /usage` and parsing output)
- Enhanced local log parsing from ~/.claude/projects/**/*.jsonl
"""

import asyncio
import glob
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.config import get_platform_config_dir, is_local_collector_enabled, settings
from app.core.utils import PaceCalculator, human_delta

logger = logging.getLogger(__name__)


class AnthropicLocalMixin:
    """
    Mixin providing CLI PTY and local log collection for Anthropic (Claude).
    Intended to be composed into AnthropicCollector.
    """

    # ──────────────────────────────── CLI PTY strategy ───────────────────────

    async def _strategy_cli_pty(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Third tier: CLI PTY fallback."""
        if not is_local_collector_enabled():
            return []
        return await self._collect_via_cli_pty()

    async def _collect_via_cli_pty(self) -> list[dict[str, Any]]:
        """
        Fetch Claude usage by running the 'claude' CLI and parsing '/usage' output.
        Uses subprocess communicate() to send /usage and capture the result.
        """
        try:
            # Check if claude CLI is available
            proc = await asyncio.create_subprocess_exec(
                "which",
                "claude",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if proc.returncode != 0:
                logger.debug("Claude CLI not found in path, skipping PTY fallback")
                return []

            process = await asyncio.create_subprocess_exec(
                "claude",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate(input=b"/usage\nexit\n")
            output = self._strip_ansi(stdout.decode(errors="ignore"))

            if not output or not any(x in output.lower() for x in ["usage", "used", "current"]):
                return []

            # Load credentials for identity and tier
            creds = None
            identity_str = ""
            tier = None
            if hasattr(self, "_get_credentials") and hasattr(self, "_extract_identity_from_oauth"):
                creds = await self._get_credentials()
                identity_str = self._extract_identity_from_oauth(creds)

                if creds:
                    raw_tier = creds.get("claudeAiOauth", {}).get("rateLimitTier")
                    if raw_tier:
                        tier_map = {
                            "tier_0": "Free",
                            "tier_1": "Pro",
                            "tier_2": "Max",
                            "tier_3": "Team",
                            "tier_4": "Enterprise",
                            "tier_5": "Enterprise",
                        }
                        tier = tier_map.get(raw_tier.lower(), raw_tier.capitalize())

            if not tier and hasattr(self, "_get_local_config_hints"):
                local_hints = self._get_local_config_hints()
                local_tier = local_hints.get("billing_tier") or local_hints.get("tier")
                if local_tier:
                    tier = str(local_tier).capitalize()

            identity_suffix = f" | {identity_str}" if identity_str else ""

            return self._parse_cli_usage_output(output, identity_suffix, tier, identity_str)

        except Exception as e:
            logger.debug(f"Claude CLI PTY fallback failed: {e}")
            return []

    def _strip_ansi(self, text: str) -> str:
        """Strip ANSI escape codes from string."""
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        return ansi_escape.sub("", text)

    def _parse_cli_usage_output(
        self,
        output: str,
        identity_suffix: str = "",
        tier: str | None = None,
        account_label: str | None = None,
    ) -> list[dict[str, Any]]:
        """Parse the text output of 'claude /usage' into quota cards."""
        results = []
        now = datetime.now(UTC)

        # Matches: "Current session: 42% used (resets in 2h 15m)"
        usage_re = re.compile(
            r"(Current\s+(?:session|week|window))\s*[:\s-]*\s*(\d+(?:\.\d+)?)\s*%\s*used"
            r"(?:\s*\(resets\s+in\s+([^)]+)\))?",
            re.IGNORECASE,
        )

        for match in usage_re.finditer(output):
            label_raw = match.group(1).strip().title()
            pct_used = float(match.group(2))
            reset_str = match.group(3)

            label_map = {
                "Current Session": "Session",
                "Current Week": "Weekly",
                "Current Window": "Session",
            }
            u_type = label_map.get(label_raw, label_raw)
            remaining_pct = 100.0 - pct_used

            # Correct window type
            w_type = (
                "session" if "Session" in u_type else "weekly" if "Week" in u_type else "unknown"
            )

            # Parse reset duration string "2h 15m", "3d 4h", etc.
            reset_at = None
            if reset_str:
                delta = timedelta()
                d_match = re.search(r"(\d+)\s*d", reset_str)
                h_match = re.search(r"(\d+)\s*h", reset_str)
                m_match = re.search(r"(\d+)\s*m", reset_str)
                if d_match:
                    delta += timedelta(days=int(d_match.group(1)))
                if h_match:
                    delta += timedelta(hours=int(h_match.group(1)))
                if m_match:
                    delta += timedelta(minutes=int(m_match.group(1)))
                if delta.total_seconds() > 0:
                    reset_at = now + delta

            results.append(
                {
                    "service_name": "Claude",
                    "icon": "🟠",
                    "remaining": f"{remaining_pct:.1f}%",
                    "unit": "capacity",
                    "reset": human_delta(reset_at) if reset_at else "Unknown",
                    "health": "good"
                    if pct_used < 70
                    else "warning"
                    if pct_used < 90
                    else "critical",
                    "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                    "detail": f"{pct_used:.1f}% used [CLI PTY]{identity_suffix}",
                    "used_value": pct_used,
                    "limit_value": 100.0,
                    "unit_type": "percent",
                    "window_type": w_type,
                    "model_id": None,
                    "tier": tier,
                    "account_label": account_label,
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": self.DATA_SOURCE_LOCAL,
                    "input_source": "server",
                    "updated_at": now.isoformat(),
                }
            )

        return results

    # ──────────────────────────────── Local log strategy ─────────────────────

    async def _strategy_local_enhanced(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fourth tier: Enhanced local logs fallback."""
        if not is_local_collector_enabled():
            return []
        return await self._get_claude_local_enhanced()

    async def _get_claude_local_enhanced(self) -> list[dict[str, Any]]:
        """
        Offloads blocking file I/O to a thread to avoid blocking the event loop.
        See _get_claude_local_enhanced_sync for implementation details.
        """
        return await asyncio.to_thread(self._get_claude_local_enhanced_sync)

    def _get_claude_local_enhanced_sync(self) -> list[dict[str, Any]]:
        """
        Synchronous implementation of local log parsing.
        Called via asyncio.to_thread — must not be awaited directly.

        Scans multiple config directories for .jsonl files and tracks all
        token types including cache reads and cache creation.

        Features:
        - Multiple config roots (CLAUDE_CONFIG_DIR comma-separated)
        - All token types: input, cache_read, cache_creation, output
        - Deduplication by message.id + requestId
        - 5-hour sliding window to match OAuth session window behavior
        """
        config_dirs = self._get_config_dirs()

        all_files = []
        for projects_dir in config_dirs:
            files = glob.glob(f"{projects_dir}/**/*.jsonl", recursive=True)
            all_files.extend(files)

        if not all_files:
            logger.debug("No Claude project log files found in any config directory")
            return []

        # Read credentials file for identity and tier info
        tier = None
        email = ""
        try:
            # 1. Check primary credentials file (.credentials.json usually)
            if os.path.exists(self._credentials_path):
                with open(self._credentials_path) as f:
                    data = json.load(f)
                    # Try claudeAiOauth subscriptionType or rateLimitTier
                    oauth = data.get("claudeAiOauth", {})
                    raw_sub = oauth.get("subscriptionType")
                    raw_tier = oauth.get("rateLimitTier")

                    if raw_sub:
                        tier = str(raw_sub).capitalize()
                    elif raw_tier:
                        tier_map = {
                            "tier_0": "Free",
                            "tier_1": "Pro",
                            "tier_2": "Max",
                            "tier_3": "Team",
                            "tier_4": "Enterprise",
                            "tier_5": "Enterprise",
                            "default_claude_ai": "Pro",
                        }
                        tier = tier_map.get(raw_tier.lower(), raw_tier.capitalize())

                    if not tier:
                        plan = data.get("account", {}).get("plan", "").lower()
                        if plan:
                            tier = plan.capitalize()

                    oauth_acc = data.get("oauthAccount", {})
                    email = oauth_acc.get("emailAddress", "") or oauth_acc.get("email", "")

            # 2. Fallback to ~/.claude.json for identity/billing hints
            if not email or not tier:
                path = os.path.expanduser("~/.claude.json")
                if os.path.exists(path):
                    with open(path) as f:
                        data = json.load(f)
                        if not email:
                            oauth_acc = data.get("oauthAccount", {})
                            email = oauth_acc.get("emailAddress", "") or oauth_acc.get("email", "")
                        if not tier:
                            oa = data.get("oauthAccount", {})
                            bt = oa.get("billingType")
                            if bt:
                                if "pro" in bt.lower():
                                    tier = "Pro"
                                elif "free" in bt.lower():
                                    tier = "Free"
                                elif bt == "default_claude_ai":
                                    tier = "Pro"  # Most likely Pro if using Claude Code
                                else:
                                    tier = str(bt).capitalize()

                            if not tier:
                                local_tier = data.get("billing_tier") or data.get("tier")
                                if local_tier:
                                    tier = str(local_tier).capitalize()
        except Exception as e:
            logger.debug(f"Could not read tier/email from credentials: {e}")

        limit = (
            settings.CLAUDE_FREE_LIMIT
            if tier == "Free"
            else settings.CLAUDE_MAX_LIMIT
            if tier == "Max"
            else settings.CLAUDE_PRO_LIMIT
        )
        cutoff_5h = datetime.now(UTC) - timedelta(hours=5)
        cutoff_7d = datetime.now(UTC) - timedelta(days=7)

        # Persist identity if found
        if email:
            self.account_label = email

        def _empty_bucket() -> dict:
            return {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_creation": 0,
                "web_search": 0,
                "web_fetch": 0,
                "sessions": set(),
                "models": {},
                "oldest": None,
            }

        session_bucket = _empty_bucket()
        weekly_bucket = _empty_bucket()
        seen_messages: set = set()

        for fpath in all_files:
            try:
                with open(fpath, encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if entry.get("type") != "assistant":
                            continue

                        ts_raw = entry.get("timestamp")
                        if not ts_raw:
                            continue

                        try:
                            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        except ValueError:
                            continue

                        if ts < cutoff_7d:
                            continue

                        msg_data = entry.get("message", {})
                        msg_id = msg_data.get("id", "")
                        request_id = msg_data.get("requestId", "")
                        dedup_key = (msg_id, request_id)

                        if dedup_key in seen_messages:
                            continue
                        seen_messages.add(dedup_key)

                        usage = msg_data.get("usage", {})
                        inp = usage.get("input_tokens", 0)
                        out = usage.get("output_tokens", 0)
                        cache_r = usage.get("cache_read_input_tokens", 0)
                        cache_w = usage.get("cache_creation_input_tokens", 0)
                        server_tools = usage.get("server_tool_use") or {}
                        web_search = server_tools.get("web_search_requests", 0) or 0
                        web_fetch = server_tools.get("web_fetch_requests", 0) or 0

                        model = msg_data.get("model", "")
                        short_model = self._short_model_id(model) if model else ""
                        session_id = entry.get("sessionId", "")
                        token_sum = inp + out + cache_r + cache_w

                        # Accumulate into weekly bucket (all messages within 7d)
                        weekly_bucket["input"] += inp
                        weekly_bucket["output"] += out
                        weekly_bucket["cache_read"] += cache_r
                        weekly_bucket["cache_creation"] += cache_w
                        weekly_bucket["web_search"] += web_search
                        weekly_bucket["web_fetch"] += web_fetch
                        if session_id:
                            weekly_bucket["sessions"].add(session_id)
                        if short_model:
                            weekly_bucket["models"][short_model] = (
                                weekly_bucket["models"].get(short_model, 0) + token_sum
                            )
                        if not weekly_bucket["oldest"] or ts < weekly_bucket["oldest"]:
                            weekly_bucket["oldest"] = ts

                        # Also accumulate into the 5h session bucket
                        if ts >= cutoff_5h:
                            session_bucket["input"] += inp
                            session_bucket["output"] += out
                            session_bucket["cache_read"] += cache_r
                            session_bucket["cache_creation"] += cache_w
                            session_bucket["web_search"] += web_search
                            session_bucket["web_fetch"] += web_fetch
                            if session_id:
                                session_bucket["sessions"].add(session_id)
                            if short_model:
                                session_bucket["models"][short_model] = (
                                    session_bucket["models"].get(short_model, 0) + token_sum
                                )
                            if not session_bucket["oldest"] or ts < session_bucket["oldest"]:
                                session_bucket["oldest"] = ts

            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning(f"Error reading Claude log file {fpath}: {e}")
                continue

        session_total = (
            session_bucket["input"]
            + session_bucket["output"]
            + session_bucket["cache_read"]
            + session_bucket["cache_creation"]
        )
        weekly_total = (
            weekly_bucket["input"]
            + weekly_bucket["output"]
            + weekly_bucket["cache_read"]
            + weekly_bucket["cache_creation"]
        )

        oldest_session = session_bucket["oldest"]
        reset_at_session = (oldest_session + timedelta(hours=5)) if oldest_session else None
        identity_suffix = f" | {email}" if email else ""

        remaining = max(0, limit - session_total)
        pct = (session_total / limit * 100) if limit > 0 else 0

        fallback_card = {
            "service_name": "Claude Pro",
            "icon": "🟠",
            "remaining": f"{remaining:,}",
            "unit": "tokens / 5h",
            "reset": human_delta(reset_at_session),
            "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
            "pace": PaceCalculator.estimate_longevity(pct, reset_at_session),
            "detail": f"{session_total:,} / {limit:,} [Local Logs] | cli-local{identity_suffix}",
            "used_value": float(session_total),
            "limit_value": float(limit),
            "is_unlimited": False,
            "tier": tier,
            "account_label": email,
            "unit_type": "tokens",
            "window_type": "session",
            "model_id": None,
            "reset_at": reset_at_session.isoformat() if reset_at_session else None,
            "data_source": self.DATA_SOURCE_LOCAL,
            "input_source": "server",
            "usage_url": "https://claude.ai/settings/usage",
            "updated_at": datetime.now(UTC).isoformat(),
        }

        results: list[dict[str, Any]] = [
            {
                "window_type": "session",
                "_enrichment_detail": self._build_enrichment_detail(session_bucket),
                "totals": {
                    "input": session_bucket["input"],
                    "output": session_bucket["output"],
                    "cache_read": session_bucket["cache_read"],
                    "cache_creation": session_bucket["cache_creation"],
                    "total": session_total,
                    "sessions": len(session_bucket["sessions"]),
                },
                "_fallback_card": fallback_card,
            }
        ]

        if weekly_total > 0:
            results.append(
                {
                    "window_type": "weekly",
                    "_enrichment_detail": self._build_enrichment_detail(weekly_bucket),
                    "totals": {
                        "input": weekly_bucket["input"],
                        "output": weekly_bucket["output"],
                        "cache_read": weekly_bucket["cache_read"],
                        "cache_creation": weekly_bucket["cache_creation"],
                        "total": weekly_total,
                        "sessions": len(weekly_bucket["sessions"]),
                    },
                }
            )

        return results

    @staticmethod
    def _short_model_id(model: str) -> str:
        """Map a full Anthropic model ID to a short display name."""
        m = model.lower()
        if "opus" in m:
            return "opus"
        if "sonnet" in m:
            return "sonnet"
        if "haiku" in m:
            return "haiku"
        if "omelette" in m or "design" in m:
            return "design"
        base = m.replace("claude-", "")
        return base.split("-")[0] if base else m

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1000:
            return f"{n // 1000}k"
        return str(n)

    def _build_enrichment_detail(self, bucket: dict) -> str:
        """Build a compact detail fragment from a token-accumulation bucket."""
        sections: list[str] = []

        token_parts = []
        if bucket["input"]:
            token_parts.append(f"in:{self._fmt_tokens(bucket['input'])}")
        if bucket["output"]:
            token_parts.append(f"out:{self._fmt_tokens(bucket['output'])}")
        if bucket["cache_read"]:
            token_parts.append(f"cache_r:{self._fmt_tokens(bucket['cache_read'])}")
        if bucket["cache_creation"]:
            token_parts.append(f"cache_w:{self._fmt_tokens(bucket['cache_creation'])}")
        if token_parts:
            sections.append(" ".join(token_parts))

        model_parts = [
            f"{mid}:{self._fmt_tokens(cnt)}"
            for mid, cnt in sorted(bucket["models"].items(), key=lambda x: -x[1])
        ]
        if model_parts:
            sections.append(" ".join(model_parts))

        if bucket["sessions"]:
            sc = len(bucket["sessions"])
            sections.append(f"{sc} session{'s' if sc != 1 else ''}")

        web_total = bucket["web_search"] + bucket["web_fetch"]
        if web_total:
            sections.append(f"web:{web_total}")

        return " | ".join(sections)

    def _get_config_dirs(self) -> list[str]:
        """
        Get list of Claude config directories to scan.

        Checks CLAUDE_CONFIG_DIR environment variable first (supports comma-separated
        paths), then falls back to platform-default locations.
        """
        dirs = []

        # Priority 1: CLAUDE_CONFIG_DIR (comma-separated)
        config_env = os.getenv("CLAUDE_CONFIG_DIR", "")
        if config_env:
            for path in config_env.split(","):
                path = path.strip()
                if path and os.path.isdir(path):
                    projects_path = (
                        os.path.join(path, "projects") if not path.endswith("/projects") else path
                    )
                    if os.path.isdir(projects_path):
                        dirs.append(projects_path)

        # Priority 2: Default locations (platform-aware)
        default_paths = [
            os.path.join(get_platform_config_dir("claude"), "projects"),
            os.path.expanduser("~/.config/claude/projects"),  # Legacy/Generic Linux
            os.path.expanduser("~/.claude/projects"),  # Legacy/Direct home
        ]

        for path in default_paths:
            if os.path.isdir(path) and path not in dirs:
                dirs.append(path)

        return dirs
