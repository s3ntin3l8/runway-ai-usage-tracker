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

from app.core.config import get_platform_config_dir, settings
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
        if not settings.LOCAL_COLLECTOR_ENABLED:
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

            stdout, stderr = await process.communicate(input=b"/usage\n")
            output = self._strip_ansi(stdout.decode(errors="ignore"))

            if not output or not any(x in output.lower() for x in ["usage", "used", "current"]):
                return []

            return self._parse_cli_usage_output(output)

        except Exception as e:
            logger.debug(f"Claude CLI PTY fallback failed: {e}")
            return []

    def _strip_ansi(self, text: str) -> str:
        """Strip ANSI escape codes from string."""
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        return ansi_escape.sub("", text)

    def _parse_cli_usage_output(self, output: str) -> list[dict[str, Any]]:
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
                "Current Session": "Session Window",
                "Current Week": "Weekly Window",
                "Current Window": "Session Window",
            }
            u_type = label_map.get(label_raw, label_raw)
            remaining_pct = 100.0 - pct_used

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
                    "service_name": f"Claude ({u_type})",
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
                    "detail": f"{pct_used:.1f}% used [CLI PTY]",
                    "used_value": pct_used,
                    "limit_value": 100.0,
                    "unit_type": "percent",
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": "cli",
                    "updated_at": now.isoformat(),
                }
            )

        return results

    # ──────────────────────────────── Local log strategy ─────────────────────

    async def _strategy_local_enhanced(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fourth tier: Enhanced local logs fallback."""
        if not settings.LOCAL_COLLECTOR_ENABLED:
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

        # Read credentials file for tier info
        tier = None
        try:
            if os.path.exists(self._credentials_path):
                with open(self._credentials_path) as f:
                    data = json.load(f)
                    plan = data.get("account", {}).get("plan", "").lower()
                    if plan:
                        tier = plan.capitalize()
        except Exception as e:
            logger.debug(f"Could not read tier from credentials: {e}")

        limit = settings.CLAUDE_FREE_LIMIT if tier == "Free" else settings.CLAUDE_PRO_LIMIT
        cutoff = datetime.now(UTC) - timedelta(hours=5)

        total_tokens = 0
        seen_messages: set = set()  # Deduplicate by (message_id, request_id)
        oldest: datetime | None = None

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

                        if ts < cutoff:
                            continue

                        msg_data = entry.get("message", {})
                        msg_id = msg_data.get("id", "")
                        request_id = msg_data.get("requestId", "")
                        dedup_key = (msg_id, request_id)

                        if dedup_key in seen_messages:
                            continue
                        seen_messages.add(dedup_key)

                        usage = msg_data.get("usage", {})
                        total_tokens += (
                            usage.get("input_tokens", 0)
                            + usage.get("output_tokens", 0)
                            + usage.get("cache_read_tokens", 0)
                            + usage.get("cache_creation_tokens", 0)
                        )

                        if not oldest or ts < oldest:
                            oldest = ts

            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning(f"Error reading Claude log file {fpath}: {e}")
                continue

        remaining = max(0, limit - total_tokens)
        pct = (total_tokens / limit * 100) if limit > 0 else 0
        reset_at = (oldest + timedelta(hours=5)) if oldest else None

        return [
            {
                "service_name": "Claude Pro",
                "icon": "🟠",
                "remaining": f"{remaining:,}",
                "unit": "tokens / 5h",
                "reset": human_delta(reset_at),
                "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                "detail": f"{total_tokens:,} / {limit:,} [Local Logs] | cli-local",
                "used_value": float(total_tokens),
                "limit_value": float(limit),
                "is_unlimited": False,
                "tier": tier,
                "unit_type": "tokens",
                "reset_at": reset_at.isoformat() if reset_at else None,
                "data_source": "local",
                "usage_url": "https://claude.ai/settings/usage",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ]

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
