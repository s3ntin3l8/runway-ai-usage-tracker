import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.models.schemas import LimitCard

logger = logging.getLogger(__name__)


_DEBOUNCE_INTERVAL = 30.0  # seconds between disk flushes


class ExternalMetricService:
    def __init__(self):
        self.path = settings.EXTERNAL_METRICS_PATH
        self._ensure_dir()
        self.metrics: dict[str, dict[str, Any]] = self._load()
        self._lock = asyncio.Lock()
        self._last_save_time: float = 0.0
        self._pending_save: bool = False

    def _ensure_dir(self):
        dir_path = os.path.dirname(self.path)
        if not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)

    def _load(self) -> dict[str, dict[str, Any]]:
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    return json.load(f)
            except FileNotFoundError:
                logger.debug(f"External metrics file not found: {self.path}")
                return {}
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in external metrics file: {self.path}")
                return {}
            except Exception as e:
                logger.error(f"Failed to load external metrics: {e}")
                return {}
        return {}

    async def _save_unlocked(self, force: bool = False):
        """Persist metrics to disk. Caller must already hold self._lock.

        Debounced: skips the write if one happened within the last
        _DEBOUNCE_INTERVAL seconds, unless force=True (used for eviction).
        When skipped, schedules a deferred flush so the pending state is not
        lost if the app idles before the next natural write.
        """
        now = time.time()
        if not force and now - self._last_save_time < _DEBOUNCE_INTERVAL:
            if not self._pending_save:
                self._pending_save = True
                remaining = _DEBOUNCE_INTERVAL - (now - self._last_save_time)
                asyncio.create_task(self._flush_after(remaining))
            return
        def sync_save():
            with open(self.path, "w") as f:
                json.dump(self.metrics, f, indent=2)
        await asyncio.to_thread(sync_save)
        self._last_save_time = now
        self._pending_save = False

    async def _flush_after(self, delay: float):
        """Flush any pending save after the debounce window expires."""
        await asyncio.sleep(delay)
        async with self._lock:
            if self._pending_save:
                await self._save_unlocked(force=True)

    async def _save(self):
        """Persist metrics to disk, acquiring the lock internally."""
        async with self._lock:
            await self._save_unlocked()

    async def update_metrics(self, provider: str, cards: list[LimitCard]):
        now = datetime.now(UTC).isoformat()
        processed_cards = []
        for card in cards:
            card_dict = card.model_dump()
            # Append update info to detail
            card_dict[
                "detail"
            ] += (
                f" [Sidecar Updated: {datetime.now(UTC).strftime('%H:%M:%S')}]"
            )
            processed_cards.append(card_dict)

        async with self._lock:
            self.metrics[provider] = {"timestamp": now, "cards": processed_cards}
            await self._save_unlocked()

    async def metrics_update_from_ingest(self, provider: str, cards: list[LimitCard]):
        """Special update for ingest that avoids double-tagging metadata."""
        now = datetime.now(UTC).isoformat()
        async with self._lock:
            self.metrics[provider] = {
                "timestamp": now,
                "cards": [card.model_dump() for card in cards],
            }
            await self._save_unlocked()

    def _aggregate_opencode_cards(
        self, opencode_cards: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Aggregate OpenCode cards from multiple hosts.

        Args:
            opencode_cards: List of card dicts from opencode-* providers

        Returns:
            List of aggregated cards (5h, week, month)
        """
        if not opencode_cards:
            return []

        # Limits for aggregated opencode windows
        limits = {
            "5h": 12.0,
            "week": 30.0,
            "month": 60.0,
        }

        # Track aggregated data per window
        aggregated: dict[str, dict[str, Any]] = {
            "5h": {"used": 0.0, "msgs": 0, "hosts": set(), "time_str": ""},
            "week": {"used": 0.0, "msgs": 0, "hosts": set(), "time_str": ""},
            "month": {"used": 0.0, "msgs": 0, "hosts": set(), "time_str": ""},
        }

        # Window name mappings
        window_map = {
            "5 Hours": "5h",
            "7 Days": "week",
            "30 Days": "month",
        }

        for card in opencode_cards:
            service = card.get("service_name", "")
            # Extract window type from service name
            window_key = None
            for window_name, key in window_map.items():
                if window_name in service:
                    window_key = key
                    break

            if window_key:
                # Parse cost and msgs from metadata (Primary) or detail (Fallback)
                metadata = card.get("metadata", {})
                used = metadata.get("used", 0.0)
                msgs = metadata.get("count", 0)

                # Fallback to string parsing if metadata missing (backward compatibility)
                if used == 0.0 and "$" in card.get("detail", ""):
                    try:
                        detail = card.get("detail", "")
                        cost_part = detail.split("$")[1].split(" used")[0]
                        used = float(cost_part)
                    except (IndexError, ValueError):
                        pass

                if msgs == 0 and " msgs" in card.get("detail", ""):
                    try:
                        detail = card.get("detail", "")
                        msgs_part = detail.split(" · ")[1].split(" msgs")[0]
                        msgs = int(msgs_part)
                    except (IndexError, ValueError):
                        pass

                # Extract hostname
                host = metadata.get("hostname")
                if not host:
                    try:
                        detail = card.get("detail", "")
                        host = detail.split(" · ")[2].split(" [Sidecar]")[0]
                    except IndexError:
                        host = card.get("_provider", "unknown")

                aggregated[window_key]["hosts"].add(host)
                aggregated[window_key]["used"] += used
                aggregated[window_key]["msgs"] += msgs
                aggregated[window_key]["time_str"] = card.get("_time_str", "")

        # Create aggregated cards for each window
        window_labels = {
            "5h": "5h Combined",
            "week": "7d Combined",
            "month": "30d Combined",
        }

        result = []
        for window, data in aggregated.items():
            if data["hosts"]:  # Only create card if we have data
                used = data["used"]
                limit = limits[window]
                remaining = max(0, limit - used)
                pct = (used / limit * 100) if limit > 0 else 0
                host_count = len(data["hosts"])
                time_str = data["time_str"]

                result.append(
                    {
                        "service_name": f"OpenCode ({window_labels[window]})",
                        "icon": "⚡",
                        "remaining": f"${remaining:.2f}",
                        "unit": f"${limit:.0f} limit",
                        "reset": f"Rolling {window}",
                        "health": (
                            "good"
                            if pct < 70
                            else "warning" if pct < 90 else "critical"
                        ),
                        "pace": (
                            "Stable" if pct < 50 else "High" if pct < 80 else "Fatigue"
                        ),
                        "detail": f"Combined from {host_count} hosts · ${used:.2f} used ({time_str})",
                    }
                )

        return result

    async def get_opencode_aggregated(self) -> list[dict[str, Any]]:
        """
        Get aggregated OpenCode metrics from sidecar data.

        Returns:
            List[Dict[str, Any]]: List of aggregated cards for 5h, week, month windows
        """
        opencode_cards = []
        now = datetime.now(UTC)

        async with self._lock:
            for provider, data in self.metrics.items():
                if provider.startswith("opencode-"):
                    ts = datetime.fromisoformat(data["timestamp"])
                    diff = now - ts
                    minutes = int(diff.total_seconds() / 60)
                    time_str = f"{minutes}m ago" if minutes > 0 else "just now"

                    for card in data["cards"]:
                        card_copy = card.copy()
                        card_copy["_provider"] = provider
                        card_copy["_time_str"] = time_str
                        opencode_cards.append(card_copy)

        return self._aggregate_opencode_cards(opencode_cards)

    async def get_all_metrics(self) -> list[dict[str, Any]]:
        all_cards = []
        opencode_cards = []  # Collect all opencode-* cards for aggregation
        now = datetime.now(UTC)
        STALE_HOURS = 2  # Drop providers silent for more than 2 hours

        async with self._lock:
            stale = [
                p for p, d in self.metrics.items()
                if (now - datetime.fromisoformat(d["timestamp"])).total_seconds() > STALE_HOURS * 3600
            ]
            for p in stale:
                del self.metrics[p]
                logger.info(f"Evicted stale external metrics for provider: {p}")
            if stale:
                await self._save_unlocked(force=True)

            for provider, data in self.metrics.items():
                ts = datetime.fromisoformat(data["timestamp"])
                diff = now - ts
                minutes = int(diff.total_seconds() / 60)

                time_str = f"{minutes}m ago" if minutes > 0 else "just now"

                # Check if this is an opencode sidecar provider
                if provider.startswith("opencode-"):
                    # Collect cards for later aggregation
                    for card in data["cards"]:
                        card_copy = card.copy()
                        card_copy["_provider"] = provider
                        card_copy["_time_str"] = time_str
                        opencode_cards.append(card_copy)
                else:
                    # Keep non-opencode cards as-is
                    for card in data["cards"]:
                        updated_card = card.copy()
                        updated_card["service_name"] += f" ({time_str})"
                        all_cards.append(updated_card)

        # Aggregate opencode cards and add to result
        all_cards.extend(self._aggregate_opencode_cards(opencode_cards))

        return all_cards


# Global instance
external_metric_service = ExternalMetricService()
