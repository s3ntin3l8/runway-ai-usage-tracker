# app/services/poller.py
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from sqlmodel import Session
from app.services.collector_manager import manager
from app.core.db import engine
from app.models.db import UsageSnapshot
from app.models.schemas import LimitCard
from typing import List, Optional

logger = logging.getLogger(__name__)

_COMPACTION_INTERVAL_POLLS = 96   # 96 × 15 min ≈ 24 hours
_SLEEP_INTERVAL = 7200            # 2 hours in seconds
_DORMANT_THRESHOLD = 3            # consecutive identical polls before sleep


class BackgroundPoller:
    def __init__(self, interval_seconds: int = 900):
        self._base_interval = interval_seconds
        self._interval = interval_seconds   # current active interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._poll_count = 0
        self._snapshot_hashes: dict[str, deque] = {}  # key → deque(maxlen=3) of hashes

    def start(self):
        """Start the background polling task."""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Background poller started with {self._base_interval}s interval.")

    async def stop(self):
        """Stop the background polling task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Background poller stopped.")

    async def _run_loop(self):
        while self._running:
            await asyncio.sleep(self._interval)
            if not self._running:
                break
            try:
                await self.poll_now()
            except Exception as e:
                logger.error(f"Error during background poll: {e}")

    def _update_sleep_state(self, cards: list) -> None:
        """Track quota hashes per account; adjust poll interval on dormancy or wake."""
        # Build one composite hash per key for this poll cycle
        poll_hashes: dict[str, list] = {}
        for card_dict in cards:
            try:
                card = LimitCard(**card_dict)
                if not card.provider_id or not card.account_id:
                    continue
                if card.data_source == "cache":
                    continue  # cached cards don't represent fresh activity
                key = f"{card.provider_id}:{card.account_id}"
                poll_hashes.setdefault(key, []).append(hash((card.used_value, card.limit_value)))
            except Exception:
                logger.debug(f"Skipping malformed card in sleep state tracking")

        # Append one composite hash per key (order-independent via sorted)
        for key, hashes in poll_hashes.items():
            composite = hash(tuple(sorted(hashes)))
            if key not in self._snapshot_hashes:
                self._snapshot_hashes[key] = deque(maxlen=_DORMANT_THRESHOLD)
            self._snapshot_hashes[key].append(composite)

        if not self._snapshot_hashes:
            return

        # Wake: any account's latest hash differs from previous
        any_changed = any(
            len(dq) >= 2 and dq[-1] != dq[-2]
            for dq in self._snapshot_hashes.values()
        )
        if any_changed:
            if self._interval != self._base_interval:
                logger.info("Activity detected — resuming normal polling interval")
                self._interval = self._base_interval
                self._snapshot_hashes.clear()
            return

        # Sleep: all accounts have been identical for _DORMANT_THRESHOLD polls
        all_dormant = all(
            len(dq) == _DORMANT_THRESHOLD and len(set(dq)) == 1
            for dq in self._snapshot_hashes.values()
        )
        if all_dormant and self._interval == self._base_interval:
            logger.info("No quota activity detected — entering sleep mode (2h interval)")
            self._interval = _SLEEP_INTERVAL

    async def poll_now(self):
        """Execute a single collection and snapshot cycle."""
        logger.info("Starting scheduled background collection...")
        cards = await manager.collect_all()

        # Update dormancy state before DB write
        self._update_sleep_state(cards)

        if not cards:
            logger.debug("No metrics collected during background poll.")
        else:
            with Session(engine) as session:
                for card_dict in cards:
                    try:
                        card = LimitCard(**card_dict)
                        if not card.provider_id or not card.account_id:
                            continue
                        if card.data_source == "cache":
                            continue
                        snapshot = UsageSnapshot(
                            provider_id=card.provider_id,
                            account_id=card.account_id,
                            account_label=card.account_label,
                            service_name=card.service_name,
                            used_value=card.used_value,
                            limit_value=card.limit_value,
                            unit_type=card.unit_type,
                            currency=card.currency,
                            tier=card.tier,
                            model_id=card.model_id,
                            window_type=card.window_type,
                            health=card.health,
                            sidecar_id=card.sidecar_id,
                            is_unlimited=card.is_unlimited,
                            data_source=card.data_source,
                            error_type=card.error_type,
                            timestamp=datetime.now(timezone.utc),
                        )
                        snapshot.raw_metadata = card.metadata
                        session.add(snapshot)
                    except Exception as e:
                        logger.error(f"Failed to map card to snapshot: {e}")

                session.commit()
                logger.info(f"Background poll complete. Snapshotted {len(cards)} metrics.")

        # Fire webhook alerts for any threshold breaches
        try:
            from app.services.webhooks import check_and_fire
            limit_cards = []
            for card_dict in cards:
                try:
                    limit_cards.append(LimitCard(**card_dict))
                except Exception:
                    pass
            if limit_cards:
                with Session(engine) as webhook_session:
                    await check_and_fire(limit_cards, webhook_session)
        except Exception as e:
            logger.error(f"Webhook check failed (non-fatal): {e}")

        # Daily compaction (every 96 polls ≈ 24h)
        self._poll_count += 1
        if self._poll_count % _COMPACTION_INTERVAL_POLLS == 0:
            try:
                from app.services.compaction import compact_snapshots
                with Session(engine) as compact_session:
                    result = compact_snapshots(compact_session)
                    logger.info(f"Daily compaction: {result}")
            except Exception as e:
                logger.error(f"Compaction failed (non-fatal): {e}")


# Global instance
poller = BackgroundPoller()
