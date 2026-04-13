import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class GeminiLocalMixin:
    """Mixin for Gemini local session log parsing."""

    async def _collect_via_logs(
        self, client: httpx.AsyncClient | None = None
    ) -> list[dict[str, Any]]:
        """Fallback: Parse Gemini usage from local session logs."""
        potential_dirs = [
            settings.GEMINI_SESSIONS_DIR,
            os.path.expanduser("~/.gemini/tmp/sessions"),
            os.path.expanduser("~/.gemini/sessions"),
            os.path.expanduser("~/.gemini/tmp"),
        ]

        files = []
        try:
            existing_dirs = [d for d in potential_dirs if os.path.isdir(d)]
            if existing_dirs:
                import glob

                results = await asyncio.gather(
                    *[asyncio.to_thread(glob.glob, f"{d}/*.jsonl") for d in existing_dirs]
                )
                for found in results:
                    files.extend(found)

            if not files:
                return []

            def process_logs(fpaths):
                total = 0
                for fpath in fpaths:
                    with open(fpath) as f:
                        for line in f:
                            u = json.loads(line).get("usage", {})
                            total += u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)
                return total

            total = await asyncio.to_thread(process_logs, files)
            return [
                {
                    "service_name": "Gemini CLI (Logs)",
                    "icon": "🔵",
                    "remaining": f"{total:,}",
                    "unit": "tokens (24h)",
                    "reset": "Rolling 24h",
                    "health": "good",
                    "pace": "Stable",
                    "detail": "Fallback: Local logs",
                    "data_source": "local",
                    "usage_url": "https://one.google.com/settings",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ]
        except Exception:
            return []
