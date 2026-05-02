# app/services/poller.py
import asyncio
import hashlib
import logging
from collections import deque
from datetime import UTC, datetime

from sqlmodel import Session

from app.core.db import engine
from app.models.db import UsageSnapshot, UsageSnapshotModel
from app.models.schemas import LimitCard
from app.services.collector_manager import manager

logger = logging.getLogger(__name__)

_COMPACTION_INTERVAL_POLLS = 96  # 96 × 15 min ≈ 24 hours
_SLEEP_INTERVAL = 7200  # 2 hours in seconds
_DORMANT_THRESHOLD = 3  # consecutive identical polls before sleep


def _extract_token_fields(card: LimitCard) -> dict:
    """Extract token fields from LimitCard for UsageSnapshot columns."""
    if not card.token_usage:
        return {
            "tokens_input": None,
            "tokens_output": None,
            "tokens_reasoning": None,
            "tokens_cache_read": None,
            "tokens_total": None,
            "msgs": card.msgs,
        }
    return {
        "tokens_input": card.token_usage.get("input"),
        "tokens_output": card.token_usage.get("output"),
        "tokens_reasoning": card.token_usage.get("reasoning"),
        "tokens_cache_read": card.token_usage.get("cache_read"),
        "tokens_total": card.token_usage.get("total"),
        "msgs": card.msgs,
    }


def _create_model_records(session, snapshot_id: int, card: LimitCard) -> None:
    """Create UsageSnapshotModel records from card.by_model."""
    if not card.by_model:
        return
    for model_id, model_data in card.by_model.items():
        tokens = model_data.get("tokens")
        if tokens:
            total = sum(tokens.get(k, 0) or 0 for k in ["input", "output", "reasoning"])
            record = UsageSnapshotModel(
                snapshot_id=snapshot_id,
                model_id=model_id,
                cost=model_data.get("cost"),
                msgs=model_data.get("msgs"),
                tokens_input=tokens.get("input"),
                tokens_output=tokens.get("output"),
                tokens_reasoning=tokens.get("reasoning"),
                tokens_cache_read=tokens.get("cache_read"),
                tokens_total=total,
            )
        else:
            record = UsageSnapshotModel(
                snapshot_id=snapshot_id,
                model_id=model_id,
                cost=model_data.get("cost"),
                msgs=model_data.get("msgs"),
            )
        session.add(record)


class BackgroundPoller:
    def __init__(self, interval_seconds: int = 900):
        self._base_interval = interval_seconds
        self._interval = interval_seconds  # current active interval
        self._task: asyncio.Task | None = None
        self._running = False
        self._poll_count = 0
        self._snapshot_hashes: dict[str, deque] = {}  # key → deque(maxlen=3) of SHA hex digests
        self._wake_event = asyncio.Event()

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
            self._wake_event.set()  # break the wait
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Background poller stopped.")

    async def _run_loop(self):
        while self._running:
            # Re-read interval in case it changed during sleep
            interval = self._interval

            # Wait for either the interval to pass or a manual 'wake' event
            try:
                # We wait on the event. If it's set, wait() returns immediately.
                # If timeout hits, we proceed to poll.
                await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
            except TimeoutError:
                # Normal sleep timeout reached
                pass

            # Reset event for next cycle
            self._wake_event.clear()

            if not self._running:
                break
            try:
                await self.poll_now()
            except Exception as e:
                logger.error(f"Error during background poll: {e}")

    def _update_sleep_state(self, cards: list) -> None:
        """Track quota state per account; adjust poll interval on dormancy or wake."""
        # Build one composite representation per key for this poll cycle
        poll_states: dict[str, list] = {}
        for card_dict in cards:
            try:
                card = LimitCard(**card_dict)
                if not card.provider_id or not card.account_id:
                    continue
                if card.data_source == "cache":
                    continue  # cached cards don't represent fresh activity
                key = f"{card.provider_id}:{card.account_id}"
                # Include service_name, window_type, and variant so two windows that happen
                # to share the same (used, limit) pair don't collapse into one dormancy signal.
                window_id = f"{card.service_name}:{card.window_type}:{card.variant or ''}"
                poll_states.setdefault(key, []).append(
                    f"{window_id}:{card.used_value}:{card.limit_value}"
                )
            except Exception:
                logger.debug("Skipping malformed card in sleep state tracking")

        # Append one composite hash per key (order-independent via sorted)
        for key, states in poll_states.items():
            # Create a stable SHA-256 digest of the sorted state list
            composite_str = "|".join(sorted(states))
            digest = hashlib.sha256(composite_str.encode()).hexdigest()

            if key not in self._snapshot_hashes:
                self._snapshot_hashes[key] = deque(maxlen=_DORMANT_THRESHOLD)
            self._snapshot_hashes[key].append(digest)

        if not self._snapshot_hashes:
            return

        # Wake: any account's latest hash differs from previous
        any_changed = any(
            len(dq) >= 2 and dq[-1] != dq[-2] for dq in self._snapshot_hashes.values()
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

    def wake(self):
        """Reset dormancy state and restore normal polling interval immediately."""
        if self._interval != self._base_interval:
            logger.info("Manual wake: restoring normal poll interval")
            self._interval = self._base_interval
        self._snapshot_hashes.clear()
        self._wake_event.set()  # interrupt the _run_loop wait

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
                            variant=card.variant,
                            health=card.health,
                            sidecar_id=card.sidecar_id,
                            is_unlimited=card.is_unlimited,
                            data_source=card.data_source,
                            error_type=card.error_type,
                            timestamp=datetime.now(UTC),
                            **_extract_token_fields(card),
                        )
                        snapshot.raw_metadata = card.metadata
                        session.add(snapshot)
                        session.flush()  # Get snapshot.id for model records
                        _create_model_records(session, snapshot.id, card)
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
