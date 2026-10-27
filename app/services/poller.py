# app/services/poller.py
import asyncio
import hashlib
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session

from app.core.db import engine
from app.models.schemas import LimitCard
from app.services.accumulator import upsert_latest_usage
from app.services.collector_manager import LOCAL_SIDECAR_ID, manager
from app.services.token_cache import TokenCache

logger = logging.getLogger(__name__)


_SLEEP_INTERVAL = 7200  # 2 hours in seconds
_DORMANT_THRESHOLD = 3  # consecutive identical polls before sleep
_FLOOR_SECONDS = 30  # minimum poller tick to prevent pathological config values
# A sidecar credential unseen for this long has stopped reporting: the server's own
# in-memory copy of it expires after the same time.
_CREDENTIAL_SILENCE = timedelta(seconds=TokenCache.DEFAULT_TTL)


def _missing_collector_reason(session: Session, provider_id: str, account_id: str) -> str:
    """Why an account's server collection has no outcome this cycle.

    The collector goes missing in two ways: it was removed, or it lost its credential
    (``is_configured()`` turned False once the sidecar's pushed copy expired). Only the
    second is the operator's to fix, and where they should look depends on it."""
    from sqlmodel import col, select

    from app.models.db import CredentialSource, SidecarDevice, SidecarRegistry
    from app.services import sidecar_devices

    gone = "its collector is no longer active"
    # A disabled source was switched off on purpose, and the "local" fallback names no machine.
    newest = session.exec(
        select(CredentialSource)
        .where(
            CredentialSource.provider_id == provider_id,
            CredentialSource.account_id == account_id,
            col(CredentialSource.sidecar_id).is_not(None),
            CredentialSource.sidecar_id != LOCAL_SIDECAR_ID,
            col(CredentialSource.enabled).is_(True),
        )
        .order_by(col(CredentialSource.last_seen).desc())
    ).first()
    if newest is None or newest.sidecar_id is None:
        return gone
    last_seen = (
        newest.last_seen if newest.last_seen.tzinfo else newest.last_seen.replace(tzinfo=UTC)
    )
    if datetime.now(UTC) - last_seen < _CREDENTIAL_SILENCE:
        # Still being reported, so the credential is not what went missing.
        return gone
    host = session.get(SidecarRegistry, newest.sidecar_id)
    name = (host.custom_name or host.hostname) if host else None
    reason = f"its credential from {name or newest.sidecar_id} stopped reporting"
    # Pairing and Keychain consent only govern browser-cookie sources; a Mac's env or file
    # credential going quiet has a different cause, so it gets no hint.
    if (
        host is not None
        and newest.source_type == "cookie"
        and (host.os_platform or "").startswith("Darwin")
    ):
        # The registry id's shape says nothing about pairing: an unpaired machine keeps its
        # device row with ``revoked`` set.
        device = (
            session.get(SidecarDevice, host.sidecar_id.removeprefix("device-"))
            if sidecar_devices.reserved_registry_id(host.sidecar_id)
            else None
        )
        if device is None or device.revoked:
            # Since Keychain consent, a Mac only contributes browser cookies once it is paired
            # and the operator has enabled the source on that machine.
            reason += "; check this Mac is paired and its Keychain source is enabled"
    return reason


class BackgroundPoller:
    def __init__(self, interval_seconds: int = 900):
        self._base_interval = interval_seconds
        self._interval = interval_seconds  # current active interval
        self._task: asyncio.Task | None = None
        self._running = False
        self._snapshot_hashes: dict[str, deque] = {}  # key → deque(maxlen=3) of SHA hex digests
        self._wake_event = asyncio.Event()

    def _compute_effective_interval(self) -> int:
        """Tick rate = max(floor, min(global_default, *enabled_per_provider_overrides)).

        Returns the smallest interval the poller needs to wake on so that no
        configured interval is silently ignored. Per-collector TTLs (set on
        SmartCollector.ttl) then gate which collectors actually re-fetch on
        each tick.
        """
        from sqlmodel import select as sqlselect

        from app.models.db import ProviderConfig, SystemConfig

        try:
            with Session(engine) as s:
                sys_cfg = s.exec(sqlselect(SystemConfig)).first()
                global_interval = (
                    sys_cfg.default_poll_interval_seconds
                    if sys_cfg and sys_cfg.default_poll_interval_seconds
                    else self._base_interval
                )
                overrides = [
                    r.poll_interval_seconds
                    for r in s.exec(sqlselect(ProviderConfig).where(ProviderConfig.enabled)).all()
                    if r.poll_interval_seconds
                ]
                candidates = [global_interval, *overrides]
                return max(_FLOOR_SECONDS, min(candidates))
        except Exception as e:
            logger.debug(f"Could not compute effective interval, using base: {e}")
            return self._base_interval

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
                logger.debug("Poller task cancelled during shutdown")
            self._task = None
        logger.info("Background poller stopped.")

    async def _run_loop(self):
        while self._running:
            # When not dormant, refresh interval from DB so config edits apply
            # on the next tick without a server restart.
            if self._interval != _SLEEP_INTERVAL:
                self._interval = self._compute_effective_interval()
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
            if self._interval == _SLEEP_INTERVAL:
                logger.info("Activity detected — resuming normal polling interval")
                self._interval = self._compute_effective_interval()
                self._snapshot_hashes.clear()
            return

        # Sleep: all accounts have been identical for _DORMANT_THRESHOLD polls
        all_dormant = all(
            len(dq) == _DORMANT_THRESHOLD and len(set(dq)) == 1
            for dq in self._snapshot_hashes.values()
        )
        if all_dormant and self._interval != _SLEEP_INTERVAL:
            logger.info("No quota activity detected — entering sleep mode (2h interval)")
            self._interval = _SLEEP_INTERVAL

    def wake(self):
        """Reset dormancy state and restore normal polling interval immediately."""
        if self._interval == _SLEEP_INTERVAL:
            logger.info("Manual wake: restoring normal poll interval")
            self._interval = self._compute_effective_interval()
        self._snapshot_hashes.clear()
        self._wake_event.set()  # interrupt the _run_loop wait

    @staticmethod
    def _validated_cards(
        session: Session, cards: list[dict[str, Any]], outcomes: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Validate private Claude proof in the same no-yield transaction as card writes."""
        accepted = []
        rejected = set()
        for card in cards:
            if getattr(
                card, "claude_source_proof", None
            ) is not None and not manager.claude_card_proof_valid(session, card):
                rejected.add((card.get("provider_id"), card.get("account_id")))
                continue
            # Only public card keys reach persistence, snapshots, alerts, or API output.
            accepted.append(dict(card))
        outcomes = [
            {
                **outcome,
                "state": "skipped",
                "reason": "credential binding changed during collection",
            }
            if (outcome.get("provider_id"), outcome.get("account_id")) in rejected
            else outcome
            for outcome in outcomes
        ]
        return accepted, outcomes

    async def poll_now(self):
        """Execute a single collection and snapshot cycle."""
        logger.info("Starting scheduled background collection...")
        cards = await manager.collect_all()
        outcomes = getattr(manager, "last_collection_outcomes", [])

        with Session(engine) as session:
            cards, outcomes = self._validated_cards(session, cards, outcomes)
            self._update_sleep_state(cards)
            for card_dict in cards:
                try:
                    provider_id = card_dict.get("provider_id")
                    upsert_latest_usage(
                        session,
                        card_dict,
                        source_id=f"server:{provider_id}" if provider_id else None,
                    )
                except Exception as e:
                    logger.error(f"Failed to upsert card to LatestUsage: {e}")

            # Defer these imports until collection runs to keep poller startup
            # independent from ORM model registration and DB initialization.
            from sqlmodel import select

            from app.models.db import LatestUsageContribution
            from app.services.account_identity import resolve_account_id
            from app.services.accumulator import (
                mark_latest_usage_source_stale,
                reconcile_latest_usage_snapshot,
            )

            outcomes_by_account: dict[tuple[str, str], dict] = {}
            for outcome in outcomes:
                provider_id = outcome.get("provider_id")
                account_id = outcome.get("account_id") or "default"
                source_id = outcome.get("source_id")
                if not provider_id or not source_id:
                    continue
                canonical_account_id = resolve_account_id(provider_id, account_id, None)
                outcomes_by_account[(provider_id, canonical_account_id)] = outcome
                state = outcome.get("state")
                if state == "complete":
                    keys = {
                        (
                            str(card.get("window_type") or ""),
                            str(card.get("variant") or "default"),
                            str(card.get("model_id") or ""),
                        )
                        for card in cards
                        if card.get("provider_id") == provider_id
                        and resolve_account_id(
                            provider_id,
                            card.get("account_id") or "default",
                            card.get("account_label"),
                        )
                        == canonical_account_id
                    }
                    reconcile_latest_usage_snapshot(
                        session,
                        provider_id=provider_id,
                        account_id=canonical_account_id,
                        source_id=source_id,
                        reported_keys=keys,
                    )
                elif state in {"failed", "skipped"}:
                    mark_latest_usage_source_stale(
                        session,
                        provider_id=provider_id,
                        source_id=source_id,
                        account_id=canonical_account_id,
                        reason=outcome.get("reason") or None,
                    )

            # A dynamic collector can disappear during sync (for example when
            # its token-cache account is removed), leaving no SmartCollector
            # outcome. Preserve its last-good contribution, but age it to stale
            # after the normal grace period so it cannot look current forever.
            server_contributions = session.exec(
                select(LatestUsageContribution.provider_id, LatestUsageContribution.account_id)
                .where(LatestUsageContribution.source_id.startswith("server:"))
                .distinct()
            ).all()
            for provider_id, account_id in server_contributions:
                if (provider_id, account_id) not in outcomes_by_account:
                    mark_latest_usage_source_stale(
                        session,
                        provider_id=provider_id,
                        source_id=f"server:{provider_id}",
                        account_id=account_id,
                        reason=_missing_collector_reason(session, provider_id, account_id),
                    )

            session.commit()
            if cards or outcomes:
                logger.info(f"Background poll complete. Snapshotted {len(cards)} metrics.")
        if not cards and not outcomes:
            logger.debug("No metrics collected during background poll.")

        # Fire webhook alerts for any threshold breaches
        try:
            from app.services.webhooks import check_and_fire

            limit_cards = []
            for card_dict in cards:
                try:
                    limit_cards.append(LimitCard(**card_dict))
                except Exception:
                    logger.debug("Skipping malformed card for webhook check", exc_info=True)
            if limit_cards:
                with Session(engine) as webhook_session:
                    await check_and_fire(limit_cards, webhook_session)
        except Exception as e:
            logger.error(f"Webhook check failed (non-fatal): {e}")

        # Fire credential-health alerts (expired/invalid credentials). Runs
        # even when `cards` is empty — Token Health is independent of
        # whatever collectors happened to return this cycle, and a fully
        # broken credential often means zero cards.
        try:
            from app.services.credential_alerts import check_credential_alerts

            with Session(engine) as credential_session:
                await check_credential_alerts(credential_session)
        except Exception as e:
            logger.error(f"Credential alert check failed (non-fatal): {e}")


# Global instance
poller = BackgroundPoller()
