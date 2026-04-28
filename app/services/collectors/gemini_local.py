import asyncio
import glob
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import is_local_collector_enabled, settings

logger = logging.getLogger(__name__)


class GeminiLocalMixin:
    """Mixin for Gemini local session log parsing."""

    def _map_model_to_class(self, model_name: str) -> str:
        """Map raw model name to card category (pro, flash, flash-lite)."""
        if not model_name:
            return "unknown"
        lower = model_name.lower()
        if "flash-lite" in lower:
            return "flash-lite"
        if "flash" in lower:
            return "flash"
        if "pro" in lower:
            return "pro"
        if "ultra" in lower:
            return "ultra"
        return model_name

    def _process_sessions(self, fpaths: list[str]) -> dict[str, Any]:
        """Process session files and aggregate tokens by model."""
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
                with open(fpath) as f:
                    content = f.read()

                session_messages = []
                if fpath.endswith(".jsonl"):
                    for line in content.strip().split("\n"):
                        if line:
                            msg = json.loads(line)
                            if msg.get("tokens"):
                                session_messages.append(msg)
                else:
                    data = json.loads(content)
                    session_messages = data.get("messages", [])

                # Track message counts per model for by_model stats
                msg_counts_by_model: dict[str, int] = {}
                for msg in session_messages:
                    if msg.get("tokens"):
                        model = msg.get("model") or "unknown"
                        msg_counts_by_model[model] = msg_counts_by_model.get(model, 0) + 1

                last_msg = None
                for msg in reversed(session_messages):
                    if msg.get("tokens"):
                        last_msg = msg
                        break

                if not last_msg:
                    continue

                last_tokens = last_msg.get("tokens", {})
                msg_timestamp = last_msg.get("timestamp")
                raw_model = last_msg.get("model") or "unknown"
                model_class = self._map_model_to_class(raw_model)

                totals["messages"].append(
                    {
                        "timestamp": msg_timestamp,
                        "tokens": last_tokens,
                        "model": raw_model,
                        "model_class": model_class,
                    }
                )

                totals["input"] += last_tokens.get("input", 0)
                totals["output"] += last_tokens.get("output", 0)
                totals["cached"] += last_tokens.get("cached", 0)
                totals["thoughts"] += last_tokens.get("thoughts", 0)
                totals["tool"] += last_tokens.get("tool", 0)
                totals["total"] += last_tokens.get("total", 0)
                totals["session_count"] += 1

                # Update by_model with actual message count (not session count)
                if raw_model not in totals["by_model"]:
                    totals["by_model"][raw_model] = {
                        "msgs": 0,
                        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0},
                    }
                bm = totals["by_model"][raw_model]
                bm["msgs"] += msg_counts_by_model.get(raw_model, 0)
                bm["tokens"]["input"] += last_tokens.get("input", 0)
                bm["tokens"]["output"] += last_tokens.get("output", 0)
                bm["tokens"]["reasoning"] += last_tokens.get("thoughts", 0)
                bm["tokens"]["cache_read"] += last_tokens.get("cached", 0)

                if model_class not in totals["model_classes"]:
                    totals["model_classes"][model_class] = {
                        "input": 0,
                        "output": 0,
                        "reasoning": 0,
                        "cache_read": 0,
                        "total": 0,
                        "session_count": 0,
                    }
                mc = totals["model_classes"][model_class]
                mc["input"] += last_tokens.get("input", 0)
                mc["output"] += last_tokens.get("output", 0)
                mc["reasoning"] += last_tokens.get("thoughts", 0)
                mc["cache_read"] += last_tokens.get("cached", 0)
                mc["total"] += last_tokens.get("total", 0)
                mc["session_count"] += 1
            except (json.JSONDecodeError, OSError) as e:
                logger.debug(f"Failed to parse session file {fpath}: {e}")

        return totals

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

            detail_parts = []
            if totals["input"]:
                detail_parts.append(f"in: {totals['input']:,}")
            if totals["output"]:
                detail_parts.append(f"out: {totals['output']:,}")
            if totals["cached"]:
                detail_parts.append(f"cached: {totals['cached']:,}")
            if totals["thoughts"]:
                detail_parts.append(f"thoughts: {totals['thoughts']:,}")

            detail_str = ", ".join(detail_parts) if detail_parts else f"{totals['total']:,} tokens"

            by_model_formatted = {}
            for model_name, model_data in totals["by_model"].items():
                by_model_formatted[model_name] = {
                    "cost": 0.0,
                    "msgs": model_data["msgs"],
                }

            enrichment_data = {
                "_enrichment_detail": f"{detail_str} | {totals['session_count']} sessions",
                "token_usage": {
                    "input": totals["input"],
                    "output": totals["output"],
                    "reasoning": totals["thoughts"],
                    "cache_read": totals["cached"],
                    "total": totals["total"],
                },
                "msgs": totals["session_count"],
                "by_model": by_model_formatted,
                "model_classes": totals["model_classes"],
                "_messages": totals["messages"],
            }

            return [
                {
                    "service_name": "Gemini CLI",
                    "window_type": "session",
                    "icon": "🔵",
                    "remaining": f"{totals['total']:,}",
                    "unit": "tokens",
                    "reset": "Rolling",
                    "health": "good",
                    "pace": "Stable",
                    "detail": f"{detail_str} | {totals['session_count']} sessions",
                    "used_value": float(totals["total"]),
                    "limit_value": 0.0,
                    "is_unlimited": True,
                    "unit_type": "tokens",
                    "data_source": self.DATA_SOURCE_LOCAL,
                    "usage_url": "https://one.google.com/settings",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "_enrichment_detail": enrichment_data["_enrichment_detail"],
                    "token_usage": enrichment_data["token_usage"],
                    "msgs": enrichment_data["msgs"],
                    "by_model": enrichment_data["by_model"],
                    "_model_classes": enrichment_data["model_classes"],
                    "_messages": enrichment_data["_messages"],
                }
            ]
        except Exception as e:
            logger.debug(f"Gemini local session parsing failed: {e}")
            return []
