# app/services/poller.py
import asyncio
import hashlib
import json
import logging
from collections import deque
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.core.db import engine
from app.models.db import LatestUsage
from app.models.schemas import LimitCard
from app.services.account_identity import resolve_account_id
from app.services.accumulator import merge_card_json
from app.services.collector_manager import manager
from app.services.window_closer import WINDOW_DURATION, close_window

logger = logging.getLogger(__name__)


def _maybe_close_previous_window(
    session: Session,
    *,
    existing: LatestUsage | None,
    provider_id: str,
    account_id: str,
    window_type: str,
    new_reset_at: datetime,
) -> int:
    """Detect if the previous window has closed and archive it into usage_windows.

    Compares new_reset_at against the reset_at stored in existing.card_json.
    If new_reset_at is strictly later (and window_type is a known duration),
    calls close_window() to capture the just-closed window's final totals.

    Returns the number of usage_windows rows inserted (0 if no window closed).

    Window types not in WINDOW_DURATION (e.g. weekly_sonnet, weekly_design)
    are intentionally skipped — they are covered as per-model rows inside the
    standard weekly close, and we have no authoritative duration for them.
    """
    if existing is None or not existing.card_json:
        return 0
    if window_type not in WINDOW_DURATION:
        return 0

    try:
        existing_data = json.loads(existing.card_json)
        existing_reset_str = existing_data.get("reset_at")
        if not existing_reset_str:
            return 0

        existing_reset_dt = datetime.fromisoformat(existing_reset_str.replace("Z", "+00:00"))

        if new_reset_at <= existing_reset_dt:
            return 0  # reset_at has not advanced — no window closed

        # Previous window closed at existing_reset_dt
        window_start = existing_reset_dt - WINDOW_DURATION[window_type]
        return close_window(
            session,
            provider_id=provider_id,
            account_id=account_id,
            window_type=window_type,
            window_start=window_start,
            window_end=existing_reset_dt,
            limit_value=existing_data.get("limit_value"),
            pct_used=existing_data.get("pct_used"),
        )
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.debug(f"Window-close check skipped for {provider_id}/{account_id}: {e}")
        return 0


_SLEEP_INTERVAL = 7200  # 2 hours in seconds
_DORMANT_THRESHOLD = 3  # consecutive identical polls before sleep


class BackgroundPoller:
    def __init__(self, interval_seconds: int = 900):
        self._base_interval = interval_seconds
        self._interval = interval_seconds  # current active interval
        self._task: asyncio.Task | None = None
        self._running = False
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
                        canonical_account_id = resolve_account_id(
                            card.provider_id, card.account_id, card.account_label
                        )

                        # Upsert into LatestUsage — the table the dashboard
                        # reads from. Wrap in a savepoint so a single bad
                        # card (e.g. unique-constraint collision) can't
                        # roll back the whole poll cycle.
                        sidecar_id = card.sidecar_id or "local"
                        variant = card.variant or "default"
                        model_id = card.model_id or ""
                        incoming_partial = card.model_dump(exclude_none=True)
                        # Always embed the canonical account_id so the card_json
                        # grouping key matches the column (fleet API groups by
                        # card_json, not by the column).
                        incoming_partial["account_id"] = canonical_account_id
                        try:
                            with session.begin_nested():
                                existing = session.exec(
                                    select(LatestUsage).where(
                                        LatestUsage.provider_id == card.provider_id,
                                        LatestUsage.account_id == canonical_account_id,
                                        LatestUsage.window_type == card.window_type,
                                        LatestUsage.variant == variant,
                                        LatestUsage.model_id == model_id,
                                    )
                                ).first()
                                # Window-close detection: if reset_at has advanced,
                                # archive the just-closed window before overwriting.
                                if existing and card.reset_at:
                                    try:
                                        new_reset_dt = datetime.fromisoformat(
                                            card.reset_at.replace("Z", "+00:00")
                                            if isinstance(card.reset_at, str)
                                            else card.reset_at.isoformat()
                                        )
                                        _maybe_close_previous_window(
                                            session,
                                            existing=existing,
                                            provider_id=card.provider_id,
                                            account_id=canonical_account_id,
                                            window_type=card.window_type,
                                            new_reset_at=new_reset_dt,
                                        )
                                    except Exception as exc:
                                        logger.debug(
                                            f"Window-close detection skipped for "
                                            f"{card.provider_id}/{canonical_account_id}: {exc}"
                                        )
                                if existing:
                                    existing.card_json = merge_card_json(
                                        existing.card_json, incoming_partial
                                    )
                                    existing.sidecar_id = sidecar_id  # update audit field
                                    existing.updated_at = datetime.now(UTC)
                                else:
                                    session.add(
                                        LatestUsage(
                                            provider_id=card.provider_id,
                                            account_id=canonical_account_id,
                                            sidecar_id=sidecar_id,
                                            window_type=card.window_type,
                                            variant=variant,
                                            model_id=model_id,
                                            card_json=merge_card_json(None, incoming_partial),
                                        )
                                    )
                        except Exception as e:
                            logger.warning(
                                f"LatestUsage upsert failed for "
                                f"{card.provider_id}/{canonical_account_id}/{card.window_type}: {e}"
                            )

                        # Evict any pre-canonicalization row stored under the raw
                        # account_id (typically "default") when resolve_account_id
                        # mapped it to a different canonical identity (e.g. an email).
                        # Avoids duplicate fleet entries for the same user.
                        raw_account_id = card.account_id or "default"
                        if raw_account_id != canonical_account_id:
                            try:
                                with session.begin_nested():
                                    stale = session.exec(
                                        select(LatestUsage).where(
                                            LatestUsage.provider_id == card.provider_id,
                                            LatestUsage.account_id == raw_account_id,
                                            LatestUsage.window_type == card.window_type,
                                            LatestUsage.variant == variant,
                                            LatestUsage.model_id == model_id,
                                        )
                                    ).first()
                                    if stale:
                                        session.delete(stale)
                            except Exception as e:
                                logger.warning(
                                    f"Stale row eviction failed for "
                                    f"{card.provider_id}/{raw_account_id}/{card.window_type}: {e}"
                                )
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


# Global instance
poller = BackgroundPoller()
