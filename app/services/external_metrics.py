# TODO(Phase 13): Remove this module when all providers emit raw events.
# As of Phase 12, ExternalMetricService is still needed for:
# - sidecar-pushed LimitCard payloads (anthropic, chatgpt, gemini enrichment fallback)
# - OpenCode cross-host aggregation (get_opencode_aggregated)
# - collector_manager.get_all_metrics (antigravity deduplication)
# Full removal requires migrating those providers to push usage_events instead of cards.
import asyncio
import copy
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.utils import safe_write_json
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

        # Snapshot metrics for non-blocking persistence
        metrics_copy = copy.deepcopy(self.metrics)

        def sync_save():
            safe_write_json(self.path, metrics_copy)

        # Fire and forget the disk write to avoid blocking callers or holding the lock.
        # This prevents memory spikes when sidecars poll frequently.
        asyncio.create_task(asyncio.to_thread(sync_save))

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
            card_dict["detail"] += f" [Sidecar Updated: {datetime.now(UTC).strftime('%H:%M:%S')}]"
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

        # Track aggregated data per account_label and window
        # aggregated[account_label][window_key] = {"used": ..., "msgs": ..., "hosts": ..., "time_str": ...}
        aggregated: dict[str, dict[str, dict[str, Any]]] = {}

        def get_default_window_data():
            return {"used": 0.0, "msgs": 0, "hosts": set(), "time_str": ""}

        # Map canonical window_type to aggregation keys.
        window_key_map = {
            "session": "5h",
            "weekly": "week",
            "monthly": "month",
        }

        # Two-pass aggregation:
        # 1. Identify best account_label for each host
        host_to_label: dict[str, str] = {}
        for card in opencode_cards:
            metadata = card.get("metadata", {})
            host = metadata.get("hostname") or card.get("_provider", "unknown")
            acc_label = card.get("account_label") or metadata.get("account_label")
            if acc_label and acc_label.lower() != "default":
                host_to_label[host] = acc_label

        # 2. Group data by canonical label
        for card in opencode_cards:
            window_key = window_key_map.get(card.get("window_type") or "")

            if window_key:
                # Parse cost and msgs from metadata (Primary) or detail (Fallback)
                metadata = card.get("metadata", {})
                used = metadata.get("used", 0.0)
                msgs = metadata.get("count", 0)
                host = metadata.get("hostname") or card.get("_provider", "unknown")

                # Resolve account label
                acc_label = card.get("account_label") or metadata.get("account_label")
                if not acc_label or acc_label.lower() == "default":
                    acc_label = host_to_label.get(host, "Default")

                if acc_label not in aggregated:
                    aggregated[acc_label] = {
                        "5h": get_default_window_data(),
                        "week": get_default_window_data(),
                        "month": get_default_window_data(),
                    }

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

                aggregated[acc_label][window_key]["hosts"].add(host)
                aggregated[acc_label][window_key]["used"] += used
                aggregated[acc_label][window_key]["msgs"] += msgs
                aggregated[acc_label][window_key]["time_str"] = card.get("_time_str", "")

        # Create aggregated cards for each window and account
        window_type_map = {"5h": "session", "week": "weekly", "month": "monthly"}

        result = []
        for acc_label, windows_data in aggregated.items():
            for window, data in windows_data.items():
                if data["hosts"]:  # Only create card if we have data
                    used = data["used"]
                    limit = limits[window]
                    remaining = max(0, limit - used)
                    pct = (used / limit * 100) if limit > 0 else 0
                    host_count = len(data["hosts"])
                    time_str = data["time_str"]

                    result.append(
                        {
                            "provider_id": "opencode",
                            "service_name": "OpenCode",
                            "variant": "Combined",
                            "window_type": window_type_map[window],
                            "icon": "⚡",
                            "remaining": f"${remaining:.2f}",
                            "unit": f"${limit:.0f} limit",
                            "reset": f"Rolling {window}",
                            "health": (
                                "good" if pct < 70 else "warning" if pct < 90 else "critical"
                            ),
                            "pace": ("Stable" if pct < 50 else "High" if pct < 80 else "Fatigue"),
                            "detail": f"Combined from {host_count} hosts · ${used:.2f} used ({time_str})",
                            "account_label": acc_label if acc_label != "Default" else None,
                        }
                    )

        return result

    def _dedupe_antigravity_cards(
        self,
        candidates: list[tuple[datetime, str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Deduplicate Antigravity cards from multiple sidecars.

        For each (service_name, account_label) pair, keeps the card from the
        most recently updated sidecar. Appends "(time_str)" to service_name.
        """
        best: dict[tuple[str, str], tuple[datetime, str, dict[str, Any]]] = {}
        for sidecar_ts, time_str, card in candidates:
            key = (card.get("service_name", ""), card.get("account_label") or "")
            if key not in best or sidecar_ts > best[key][0]:
                best[key] = (sidecar_ts, time_str, card)

        result = []
        for sidecar_ts, time_str, card in best.values():
            updated = card.copy()
            updated["service_name"] = f"{updated['service_name']} ({time_str})"
            result.append(updated)
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

    async def get_provider_metrics(self, provider_id: str) -> list[dict[str, Any]]:
        """
        Get all metrics for a specific provider from all sidecar payloads.
        Used by collectors for enrichment (tier 1 fallback).
        """
        results = []
        now = datetime.now(UTC)

        async with self._lock:
            for provider_key, data in self.metrics.items():
                ts = datetime.fromisoformat(data["timestamp"])
                diff = now - ts
                minutes = int(diff.total_seconds() / 60)
                time_str = f"{minutes}m ago" if minutes > 0 else "just now"

                for card in data.get("cards", []):
                    if card.get("provider_id") == provider_id:
                        card_copy = card.copy()

                        # Ensure _enrichment_detail is present (legacy sidecar fallback)
                        if not card_copy.get("_enrichment_detail"):
                            card_copy["_enrichment_detail"] = card_copy.get("detail", "")

                        # Append source info to detail if not already present
                        detail = card_copy.get("detail", "")
                        if "[Sidecar]" not in detail:
                            card_copy["detail"] = f"{detail} · {time_str} [Sidecar]"

                        results.append(card_copy)
        return results

    async def get_all_metrics(self) -> list[dict[str, Any]]:
        all_cards: list[dict[str, Any]] = []
        opencode_cards: list[dict[str, Any]] = []
        # (sidecar_timestamp, time_str, card_dict)
        antigravity_candidates: list[tuple[datetime, str, dict[str, Any]]] = []
        now = datetime.now(UTC)
        STALE_HOURS = 2

        async with self._lock:
            stale = [
                p
                for p, d in self.metrics.items()
                if (now - datetime.fromisoformat(d["timestamp"])).total_seconds()
                > STALE_HOURS * 3600
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

                if provider.startswith("opencode-"):
                    for card in data["cards"]:
                        card_copy = card.copy()
                        card_copy["_provider"] = provider
                        card_copy["_time_str"] = time_str
                        opencode_cards.append(card_copy)
                else:
                    # Separate antigravity cards from the rest
                    sidecar_ag: list[dict[str, Any]] = []
                    for card in data["cards"]:
                        if card.get("provider_id") == "antigravity":
                            sidecar_ag.append(card)
                        else:
                            updated = card.copy()
                            updated["service_name"] += f" ({time_str})"
                            all_cards.append(updated)

                    if sidecar_ag:
                        # Within one sidecar, file-fallback cards (no account_label) inherit
                        # the label from LSP cards in the same batch.
                        known_label = next(
                            (c["account_label"] for c in sidecar_ag if c.get("account_label")),
                            None,
                        )
                        for card in sidecar_ag:
                            card = card.copy()
                            if not card.get("account_label") and known_label:
                                card["account_label"] = known_label
                            antigravity_candidates.append((ts, time_str, card))

        all_cards.extend(self._dedupe_antigravity_cards(antigravity_candidates))
        all_cards.extend(self._aggregate_opencode_cards(opencode_cards))
        return all_cards


# Global instance
external_metric_service = ExternalMetricService()
