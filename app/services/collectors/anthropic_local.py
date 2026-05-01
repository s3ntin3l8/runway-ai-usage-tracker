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

from app.core.config import get_platform_config_dir, is_local_collector_enabled
from app.core.utils import PaceCalculator, human_delta
from app.services.collectors.base import format_token_details

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

        Emits one aggregate enrichment dict per window (session, weekly) plus
        one per-model dict for each model with usage in the weekly window.
        The base class matcher uses model_id to route the right data to the
        right primary card.
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
        email = ""
        try:
            if os.path.exists(self._credentials_path):
                with open(self._credentials_path) as f:
                    data = json.load(f)
                    oauth_acc = data.get("oauthAccount", {})
                    email = oauth_acc.get("emailAddress", "") or oauth_acc.get("email", "")

            if not email:
                path = os.path.expanduser("~/.claude.json")
                if os.path.exists(path):
                    with open(path) as f:
                        data = json.load(f)
                        oauth_acc = data.get("oauthAccount", {})
                        email = oauth_acc.get("emailAddress", "") or oauth_acc.get("email", "")
        except Exception as e:
            logger.debug(f"Could not read email from credentials: {e}")

        if email:
            self.account_label = email

        # Parse all assistant messages into a flat list.
        # We keep all messages (no fixed cutoff here) and filter by the actual
        # reset_at discovered from primary cards during aggregation.
        messages: list[dict[str, Any]] = []
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

                        msg_data = entry.get("message", {})
                        msg_id = msg_data.get("id", "")
                        request_id = msg_data.get("requestId", "")
                        dedup_key = (msg_id, request_id)

                        if dedup_key in seen_messages:
                            continue
                        seen_messages.add(dedup_key)

                        usage = msg_data.get("usage", {})
                        model = msg_data.get("model", "")
                        short_model = self._short_model_id(model) if model else ""

                        messages.append(
                            {
                                "ts": ts,
                                "model_id": short_model,
                                "tokens": {
                                    "input": usage.get("input_tokens", 0),
                                    "output": usage.get("output_tokens", 0),
                                    "cache_read": usage.get("cache_read_input_tokens", 0),
                                    "cache_creation": usage.get("cache_creation_input_tokens", 0),
                                },
                                "session_id": entry.get("sessionId", ""),
                            }
                        )
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning(f"Error reading Claude log file {fpath}: {e}")
                continue

        if not messages:
            return []

        messages.sort(key=lambda m: m["ts"])

        # Use primary-discovered reset_at when available.
        now = datetime.now(UTC)
        window_resets = getattr(self, "_window_resets", {})

        def _window_start(reset_dt: datetime | None, duration: timedelta) -> datetime:
            if reset_dt is None:
                return now - duration
            if reset_dt < now:
                # If reset time is in the past, it was the START of the current window.
                return reset_dt
            # If reset time is in the future, the window started duration ago from then.
            start = reset_dt - duration
            # Cap at "now" so we don't exclude messages when reset_at is far
            # in the future (e.g. daily reset at midnight = 15h away).
            if start > now:
                return now - duration
            return start

        def _aggregate(msgs: list[dict], model_filter: str | None = None) -> dict[str, Any]:
            bucket = {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_creation": 0,
                "sessions": set(),
                "models": {},
                "model_msgs": {},
                "msg_count": 0,
            }
            for m in msgs:
                if model_filter and m["model_id"] != model_filter:
                    continue
                t = m["tokens"]
                bucket["input"] += t["input"]
                bucket["output"] += t["output"]
                bucket["cache_read"] += t["cache_read"]
                bucket["cache_creation"] += t["cache_creation"]
                if m["session_id"]:
                    bucket["sessions"].add(m["session_id"])
                mid = m["model_id"]
                if mid:
                    token_sum = t["input"] + t["output"] + t["cache_read"] + t["cache_creation"]
                    bucket["models"][mid] = bucket["models"].get(mid, 0) + token_sum
                    bucket["model_msgs"][mid] = bucket["model_msgs"].get(mid, 0) + 1
                bucket["msg_count"] += 1
            return bucket

        def _build_enrichment(bucket: dict, wt: str, mid: str | None) -> dict[str, Any] | None:
            if bucket["msg_count"] == 0:
                return None
            # Input already includes cache_read and cache_creation tokens in Anthropic logs.
            # UPDATE: Actually, input_tokens in logs EXCLUDES cached tokens.
            # To align with the universal contract, we sum them back into input.
            total_input = bucket["input"] + bucket["cache_read"] + bucket["cache_creation"]
            total = total_input + bucket["output"]
            by_model = {
                m: {"cost": 0.0, "msgs": c} for m, c in bucket["model_msgs"].items() if c > 0
            }

            token_usage = {
                "input": total_input,
                "output": bucket["output"],
                "reasoning": 0,
                "cache_read": bucket["cache_read"],
                "total": total,
            }

            s_name = "Claude Design" if mid == "design" else "Claude"

            return {
                "service_name": s_name,
                "window_type": wt,
                "model_id": mid,
                "_enrichment_detail": self._build_enrichment_detail(bucket, mid, token_usage),
                "token_usage": token_usage,
                "by_model": by_model,
                "msgs": bucket["msg_count"],
            }

        results: list[dict[str, Any]] = []

        # 1. Aggregate session enrichment (aggregate only)
        session_reset = window_resets.get(("session", None))
        session_cutoff = _window_start(session_reset, timedelta(hours=5))
        session_msgs = [m for m in messages if m["ts"] >= session_cutoff]
        session_agg = _aggregate(session_msgs)
        sess_enrich = _build_enrichment(session_agg, "session", None)
        if sess_enrich:
            results.append(sess_enrich)

        # 2. Aggregate weekly enrichment (aggregate only)
        weekly_reset = window_resets.get(("weekly", None))
        weekly_cutoff = _window_start(weekly_reset, timedelta(days=7))
        weekly_msgs = [m for m in messages if m["ts"] >= weekly_cutoff]
        weekly_agg = _aggregate(weekly_msgs)
        weekly_enrich = _build_enrichment(weekly_agg, "weekly", None)
        if weekly_enrich:
            results.append(weekly_enrich)

        # 3. Per-model weekly enrichment for cards that exist
        # Discover all models seen in the weekly window (using aggregate reset as baseline discovery)
        weekly_model_ids = {m["model_id"] for m in weekly_msgs if m["model_id"]}
        for mid in sorted(weekly_model_ids):
            # Use model-specific reset if available, fallback to aggregate weekly reset.
            m_reset = window_resets.get(("weekly", mid)) or weekly_reset
            m_cutoff = _window_start(m_reset, timedelta(days=7))
            m_msgs = [m for m in messages if m["ts"] >= m_cutoff]

            model_bucket = _aggregate(m_msgs, mid)
            model_enrich = _build_enrichment(model_bucket, "weekly", mid)
            if model_enrich:
                results.append(model_enrich)

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

    def _build_enrichment_detail(
        self, bucket: dict, model_id: str | None, token_usage: dict
    ) -> str:
        """Build a compact detail fragment from a token-accumulation bucket.

        When model_id is provided, only include stats for that model.
        """
        sections: list[str] = []

        # Use shared formatter for token counts
        tok_str = format_token_details(token_usage)
        if tok_str:
            sections.append(tok_str)

        if model_id:
            # Model-specific card: show only this model msg count
            cnt = bucket["model_msgs"].get(model_id, 0)
            if cnt > 0:
                sections.append(f"{model_id}: {cnt} msgs")
        else:
            # Aggregate card: show model breakdown
            model_parts = []
            for mid, tokens in sorted(bucket["models"].items(), key=lambda x: -x[1]):
                val = tokens
                if val >= 1_000_000:
                    val_str = f"{val / 1_000_000:.1f}M"
                elif val >= 1000:
                    val_str = f"{val // 1000}k"
                else:
                    val_str = str(val)
                model_parts.append(f"{mid}:{val_str}")
            if model_parts:
                sections.append(" ".join(model_parts))

        if bucket["sessions"]:
            sc = len(bucket["sessions"])
            sections.append(f"{sc} session{'s' if sc != 1 else ''}")

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
