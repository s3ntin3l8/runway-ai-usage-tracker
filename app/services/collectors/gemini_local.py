import asyncio
import glob
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.config import is_local_collector_enabled, settings
from app.services.collectors.base import format_token_details

logger = logging.getLogger(__name__)


class GeminiLocalMixin:
    """Mixin for Gemini local session log parsing."""

    def _map_model_to_class(self, model_name: str) -> str:
        """Map raw model name to card category (pro, flash, flash-lite)."""
        if not model_name:
            return "unknown"
        lower = model_name.lower()
        if "flash-lite" in lower or "gemini-1.5-flash-lite" in lower:
            return "flash-lite"
        if "flash" in lower or "gemini-1.5-flash" in lower:
            return "flash"
        if "pro" in lower or "gemini-1.5-pro" in lower:
            return "pro"
        if "ultra" in lower:
            return "ultra"
        return model_name

    def _process_sessions(self, fpaths: list[str]) -> dict[str, Any]:
        """Process session files and aggregate tokens by model using message deltas."""
        totals = {
            "input": 0,
            "output": 0,
            "cached": 0,
            "thoughts": 0,
            "tool": 0,
            "total": 0,
            "session_count": 0,
            "by_model": {},
            "model_classes": {},
            "messages": [],
        }

        for fpath in fpaths:
            try:
                session_messages = []
                if fpath.endswith(".jsonl"):
                    with open(fpath) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    msg = json.loads(line)
                                    # Only keep gemini responses with tokens
                                    if msg.get("type") == "gemini" and msg.get("tokens"):
                                        session_messages.append(msg)
                                except json.JSONDecodeError:
                                    continue
                else:
                    with open(fpath) as f:
                        try:
                            data = json.load(f)
                            raw_msgs = data.get("messages", [])
                            session_messages = [
                                m for m in raw_msgs if m.get("type") == "gemini" and m.get("tokens")
                            ]
                        except json.JSONDecodeError:
                            continue

                if not session_messages:
                    continue

                # Track unique session files
                totals["session_count"] += 1

                for msg in session_messages:
                    raw_tokens = msg.get("tokens", {})
                    raw_model = msg.get("model") or "unknown"
                    model_class = self._map_model_to_class(raw_model)

                    # Raw turn values
                    msg_input = raw_tokens.get("input", 0)
                    msg_output = raw_tokens.get("output", 0)
                    msg_cached = raw_tokens.get("cached", 0)
                    msg_thoughts = raw_tokens.get("thoughts", 0)
                    msg_tool = raw_tokens.get("tool", 0)
                    msg_total = raw_tokens.get("total", 0)

                    # Update overall totals (Additive Consumption)
                    totals["input"] += msg_input
                    totals["output"] += msg_output
                    totals["cached"] += msg_cached
                    totals["thoughts"] += msg_thoughts
                    totals["tool"] += msg_tool
                    totals["total"] += msg_total

                    # Update model_id totals (primary keys for card merging)
                    if model_class not in totals["model_classes"]:
                        totals["model_classes"][model_class] = {
                            "input": 0,
                            "output": 0,
                            "reasoning": 0,
                            "cache_read": 0,
                            "total": 0,
                            "msgs": 0,
                        }
                    mc = totals["model_classes"][model_class]
                    mc["input"] += msg_input
                    mc["output"] += msg_output
                    mc["reasoning"] += msg_thoughts
                    mc["cache_read"] += msg_cached
                    mc["total"] += msg_total
                    mc["msgs"] += 1

                    # Update per-raw-model stats for by_model breakdown
                    if raw_model not in totals["by_model"]:
                        totals["by_model"][raw_model] = {
                            "msgs": 0,
                            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0},
                        }
                    bm = totals["by_model"][raw_model]
                    bm["msgs"] += 1
                    bm["tokens"]["input"] += msg_input
                    bm["tokens"]["output"] += msg_output
                    bm["tokens"]["reasoning"] += msg_thoughts
                    bm["tokens"]["cache_read"] += msg_cached

                    totals["messages"].append(
                        {
                            "timestamp": msg.get("timestamp"),
                            "tokens": {
                                "input": msg_input,
                                "output": msg_output,
                                "cached": msg_cached,
                                "thoughts": msg_thoughts,
                                "tool": msg_tool,
                                "total": msg_total,
                            },
                            "model": raw_model,
                            "model_class": model_class,
                            "sessionId": msg.get("sessionId"),
                        }
                    )

            except OSError as e:
                logger.debug(f"Failed to read session file {fpath}: {e}")

        return totals

    def _aggregate_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate a list of messages into token totals and by_model stats."""
        agg = {
            "input": 0,
            "output": 0,
            "cached": 0,
            "thoughts": 0,
            "tool": 0,
            "total": 0,
            "session_count": 0,
            "by_model": {},
        }
        seen_sessions: set[str] = set()

        for m in messages:
            t = m["tokens"]
            agg["input"] += t.get("input", 0)
            agg["output"] += t.get("output", 0)
            agg["cached"] += t.get("cached", 0)
            agg["thoughts"] += t.get("thoughts", 0)
            agg["tool"] += t.get("tool", 0)
            agg["total"] += t.get("total", 0)

            sess_id = m.get("sessionId") or m.get("timestamp")
            if sess_id and sess_id not in seen_sessions:
                seen_sessions.add(sess_id)
                agg["session_count"] += 1

            raw_model = m["model"]
            if raw_model not in agg["by_model"]:
                agg["by_model"][raw_model] = {
                    "msgs": 0,
                    "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0},
                }
            bm = agg["by_model"][raw_model]
            bm["msgs"] += 1
            bm["tokens"]["input"] += t.get("input", 0)
            bm["tokens"]["output"] += t.get("output", 0)
            bm["tokens"]["reasoning"] += t.get("thoughts", 0)
            bm["tokens"]["cache_read"] += t.get("cached", 0)

        return agg

    def _build_enrichment_dict(self, model_id: str | None, agg: dict[str, Any]) -> dict[str, Any]:
        """Build a canonical enrichment dict from aggregated message data."""
        token_usage = {
            "input": agg.get("input", 0),
            "output": agg.get("output", 0),
            "cache_read": agg.get("cached", 0),
            "reasoning": agg.get("thoughts", 0),
            "total": agg.get("total", 0),
        }
        detail_str = format_token_details(token_usage) or f"{agg['total']:,} tokens"

        by_model_formatted = {}
        for model_name, model_data in agg["by_model"].items():
            tokens = model_data.get("tokens", {})
            by_model_formatted[model_name] = {
                "cost": 0.0,
                "msgs": model_data["msgs"],
                "tokens": {
                    "input": tokens.get("input", 0),
                    "output": tokens.get("output", 0),
                    "reasoning": tokens.get("reasoning", 0),
                    "cache_read": tokens.get("cache_read", 0),
                    "total": (
                        tokens.get("input", 0)
                        + tokens.get("output", 0)
                        + tokens.get("reasoning", 0)
                    ),
                },
            }

        return {
            "service_name": "Gemini",
            "model_id": model_id,
            "_enrichment_detail": f"{detail_str} | {agg['session_count']} sessions",
            "token_usage": {
                "input": agg["input"],
                "output": agg["output"],
                "reasoning": agg["thoughts"],
                "cache_read": agg["cached"],
                "total": agg["total"],
            },
            "msgs": agg["session_count"],
            "by_model": by_model_formatted,
        }

    async def _collect_via_logs(
        self, client: httpx.AsyncClient | None = None
    ) -> list[dict[str, Any]]:
        """Parse Gemini usage from local session JSON files."""
        if not is_local_collector_enabled():
            return []

        potential_dirs = [
            settings.GEMINI_SESSIONS_DIR,
            os.path.expanduser("~/.gemini/tmp/ai-usage-tracker/chats"),
            os.path.expanduser("~/.gemini/tmp/gemini/chats"),
            os.path.expanduser("~/.gemini/tmp/sessions"),
            os.path.expanduser("~/.gemini/sessions"),
        ]

        session_files = []
        try:
            existing_dirs = [d for d in potential_dirs if os.path.isdir(d)]
            if existing_dirs:
                results = await asyncio.gather(
                    *[asyncio.to_thread(glob.glob, f"{d}/session-*.json") for d in existing_dirs],
                    *[asyncio.to_thread(glob.glob, f"{d}/session-*.jsonl") for d in existing_dirs],
                )
                for found in results:
                    session_files.extend(found)

            if not session_files:
                return []

            totals = await asyncio.to_thread(self._process_sessions, session_files)

            if totals["total"] == 0:
                return []

            messages = totals["messages"]
            window_resets = getattr(self, "_window_resets", {})

            results: list[dict[str, Any]] = []
            all_model_classes = {m["model_class"] for m in messages}

            for model_class in sorted(all_model_classes):
                model_messages = [m for m in messages if m["model_class"] == model_class]

                reset_dt = window_resets.get(model_class)
                if reset_dt:
                    now = datetime.now(UTC)
                    # Roll forward if the reset time is in the past
                    while reset_dt < now:
                        reset_dt += timedelta(hours=24)

                    window_start = reset_dt - timedelta(hours=24)
                    if window_start > now:
                        window_start = now - timedelta(hours=24)

                    filtered = []
                    for m in model_messages:
                        ts = m.get("timestamp")
                        if not ts:
                            continue
                        try:
                            msg_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if msg_dt >= window_start:
                                filtered.append(m)
                        except (ValueError, TypeError):
                            continue
                    model_messages = filtered

                if not model_messages:
                    continue

                agg = self._aggregate_messages(model_messages)
                results.append(self._build_enrichment_dict(model_class, agg))

            return results
        except Exception as e:
            logger.debug(f"Gemini local session parsing failed: {e}")
            return []
