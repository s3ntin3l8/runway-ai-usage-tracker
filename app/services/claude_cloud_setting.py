"""The Claude cloud-runner collector's on/off switch.

The stored ``SystemConfig.claude_cloud_collector_enabled`` wins; ``None`` falls back to the
``CLAUDE_CLOUD_COLLECTOR_ENABLED`` environment default. The interval stays environment-only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlmodel import Session, select

from app.core.config import settings
from app.models.db import SystemConfig

# Serialises start/stop so two quick toggles cannot interleave (stop() awaits the cancel).
_switch_lock = asyncio.Lock()


def claude_cloud_collector_enabled(session: Session) -> bool:
    cfg = session.exec(select(SystemConfig)).first()
    stored = cfg.claude_cloud_collector_enabled if cfg else None
    return settings.CLAUDE_CLOUD_COLLECTOR_ENABLED if stored is None else bool(stored)


async def apply_claude_cloud_collector_setting(collector: Any, enabled: bool) -> None:
    """Start or stop the running collector; both are idempotent. Deletes no events or checkpoints."""
    if collector is None:
        return
    async with _switch_lock:
        if enabled:
            collector.start()
        else:
            await collector.stop()
