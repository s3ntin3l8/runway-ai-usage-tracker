"""Source failover for CollectorManager.

Move-only extract of candidate discovery, ordering, and the failover loop.
Publication and identity binding stay on CollectorManager.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.utils import scrub_log
from app.services.credential_sources import is_sidecar_source
from app.services.source_outcome import source_outcome
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

IDENTITY_VERIFIER_SUFFIX = ":identity-pending"


def is_verifier_key(key: str) -> bool:
    """Whether a smart-collector key is an identity-verification run (not a credential)."""
    return key.endswith(IDENTITY_VERIFIER_SUFFIX)


class CollectorFailover:
    """Candidate discovery, ordering, and the source-failover loop.

    CollectorManager keeps the registry, preference state, health stamping,
    and the Claude publication/identity cluster this loop calls back into.
    """

    def __getattr__(self, name: str) -> Any:
        """Methods that stay on CollectorManager are reached through the subclass.

        Declaring ``__getattr__`` tells the type checker those callbacks exist
        without shadowing them: instance and subclass lookup still wins.
        """
        raise AttributeError(name)

    @staticmethod
    async def _source_candidates(
        provider_id: object, account_id: object, identity_verification: bool
    ) -> list[dict[str, Any]]:
        if not isinstance(provider_id, str) or not isinstance(account_id, str):
            return []
        if identity_verification:
            # Pending bundles may sit in a slot other than ``default`` (Anthropic keys them
            # by source id); each carries its ``account_slot``.
            return await token_cache.get_pending_sources(provider_id)
        slot_candidates = await token_cache.get_source_candidates(provider_id, account_id)
        if account_id != "default":
            return slot_candidates
        # Sidecar ingest files bundles under the resolved identity, never under
        # ``default``. Without this sweep a default collector finds nothing,
        # silently degrades to a single unpinned ``collect()`` (no cross-source
        # failover, merged-cache reads only) — the 2026-10-02 Antigravity
        # incident, where a dead merged token was served while a live source
        # bundle sat unused under its identity slot. Identity-pending rows from
        # the sweep are dropped by the pending filter in the caller.
        # Two separate cache reads with no shared lock between them: a push
        # that lands in between appears in exactly one of the two sets (never
        # a duplicate pin), and the dedupe below only covers ids present in
        # both snapshots.
        swept = await token_cache.get_account_source_candidates(provider_id)
        # Defensive: the two lookups are slot-scoped today, but if some future
        # path files the same source_id under both the default slot and an
        # identity slot, failover must not attempt that bundle twice.
        seen = {candidate["source_id"] for candidate in slot_candidates}
        return slot_candidates + [
            candidate for candidate in swept if candidate["source_id"] not in seen
        ]

    def _ordered_candidates(
        self,
        provider_id: str,
        account_id: str,
        candidates: list[dict[str, Any]],
        *,
        include_resting: bool = False,
    ) -> list[dict[str, Any]]:
        """Drop disabled sources and order the rest for failover.

        A bundle whose access token is already dead cannot answer this poll, so
        it goes behind every live (or undatable) bundle even when the operator
        ranked it first: priority orders usable credentials, it must not hand
        the first attempt to a credential that is known dead and can only
        reject the call (#474). Freshness only splits candidates into
        live-then-expired; within each group the configured (priority,
        source_id) order still decides.

        Preferences are keyed by each candidate's own ``account_slot`` — where
        the bundle was filed (the resolved identity) — falling back to the
        requesting collector's ``account_id`` when that slot has no row for
        the source, so a preference set from e.g. the default account view
        still applies to candidates the sweep brought in.
        """

        def candidate_preference(candidate: dict[str, Any]) -> tuple[bool, int] | None:
            source_id = candidate["source_id"]
            slot = candidate.get("account_slot") or account_id
            keys = [(provider_id, slot)]
            if slot != account_id:
                keys.append((provider_id, account_id))
            for key in keys:
                row = self._credential_source_preferences.get(key, {})
                if source_id in row:
                    return row[source_id]
            return None

        def candidate_enabled(candidate: dict[str, Any]) -> bool:
            preference = candidate_preference(candidate)
            if preference is not None:
                return bool(preference[0])
            return bool(candidate.get("enabled", True))

        def candidate_priority(candidate: dict[str, Any]) -> int:
            preference = candidate_preference(candidate)
            if preference is not None:
                return int(preference[1])
            return int(candidate.get("priority", 0))

        def candidate_state(candidate: dict[str, Any]) -> tuple[str, datetime | None]:
            slot = candidate.get("account_slot") or account_id
            for key in ((provider_id, slot), (provider_id, account_id)):
                state = self._credential_source_state.get(key, {}).get(candidate["source_id"])
                if state is not None:
                    return state
            return ("healthy", None)

        ordered = [candidate for candidate in candidates if candidate_enabled(candidate)]
        ordered.sort(
            key=lambda candidate: (
                self._access_token_expired(candidate),
                candidate_priority(candidate),
                bool(
                    provider_id == "anthropic"
                    and not candidate.get("tokens", {}).get("oauth_token")
                ),
                candidate["source_id"],
            )
        )
        # A source whose last attempt was rejected rests between retries (15 min doubling to
        # 6 h), so it stops costing an upstream call at the head of every cycle. When the rest
        # is over it is simply due: it is tried in its normal place, and a success brings it
        # back while another failure doubles the rest. (Demoting it instead would starve it —
        # failover stops at the first source that works.) The last credential standing is
        # never starved: if every candidate is resting, the best one is still probed.
        now = datetime.now(UTC)

        def resting(candidate: dict[str, Any]) -> bool:
            retry_at = candidate_state(candidate)[1]
            if retry_at is None:
                return False
            return (retry_at if retry_at.tzinfo else retry_at.replace(tzinfo=UTC)) > now

        if include_resting:
            return ordered  # a probe looks at every enabled source, rested or not
        awake = [candidate for candidate in ordered if not resting(candidate)]
        return awake or ordered[:1]

    async def _collect_with_source_failover(
        self,
        key: str,
        client: httpx.AsyncClient,
        health_updates: dict[str, str],
    ) -> list[dict[str, Any]]:
        smart = self.smart_collectors[key]
        collector = smart.collector
        provider_id = getattr(collector, "PROVIDER_ID", None)
        account_id = (
            getattr(collector, "credential_account_id", None)
            or getattr(collector, "account_id", None)
            or "default"
        )
        default_account_label = getattr(collector, "account_label", None)
        identity_verification = is_verifier_key(key)
        candidates: list[dict[str, Any]] = []
        if isinstance(provider_id, str):
            candidates = await self._source_candidates(
                provider_id, account_id, identity_verification
            )
            # A regular default collector must not race the verifier for pending
            # sources. The dedicated collector sees only pending sidecar bundles.
            candidates = [
                candidate
                for candidate in candidates
                if bool(candidate.get("identity_pending")) == identity_verification
            ]
        if not candidates or not isinstance(provider_id, str):
            if identity_verification:
                return []
            return await asyncio.wait_for(smart.collect(client), timeout=25.0)

        # Ownership is judged against every source of the account (a pasted config
        # bundle can hold a machine's refresh secret), including ones filtered out below.
        all_candidates = list(candidates)
        candidates = await self._due_for_verification(
            identity_verification,
            provider_id,
            self._ordered_candidates(provider_id, account_id, candidates),
        )
        if not candidates:
            return []

        successful_result: list[dict[str, Any]] | None = None
        last_kept_failure_result: list[dict[str, Any]] | None = None
        skipped_for_renewal = False
        renewal_candidate: dict[str, Any] = {}
        attempted: list[dict[str, Any]] = []
        deadline = asyncio.get_running_loop().time() + 25.0
        await smart.reset()
        for index, candidate in enumerate(candidates):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            if index:
                await smart.reset()
            self._reset_attempt_identity(collector, account_id, default_account_label)
            if self._awaiting_machine_renewal(provider_id, candidate, all_candidates):
                # An idle CLI let its access token lapse. The machine renews it (the server
                # must not: rotating its refresh token would sign that CLI out), so calling
                # the API now could only 401 and flag a healthy login as revoked. No health
                # update: the row keeps its last real outcome and reads "expired".
                logger.info(
                    "Skipping %s/%s source %s: expired, waiting for its machine to renew it",
                    scrub_log(provider_id),
                    scrub_log(account_id),
                    scrub_log(candidate["source_id"]),
                )
                skipped_for_renewal = True
                renewal_candidate = candidate
                continue
            cache_slot = candidate.get("account_slot") or account_id
            attempted.append(candidate)
            async with token_cache.using_source(
                provider_id, cache_slot, candidate["source_id"]
            ) as attempt:
                result: list[dict[str, Any]] = []
                try:
                    result = await asyncio.wait_for(smart.collect(client), timeout=remaining)
                except Exception:
                    logger.exception(
                        "Credential source collection failed for %s/%s (%s)",
                        scrub_log(provider_id),
                        scrub_log(account_id),
                        scrub_log(candidate["source_id"]),
                    )
                    health_updates[candidate["source_id"]] = "unavailable"
                    # Do not retain a result from a crashed collector; returned
                    # failure cards are handled below, after collection completes.
                    continue
            # A failed fetch may still carry cached or partial usable cards; fail over only
            # when the response has no usable card at all (the on-demand source probe
            # classifies with the same rule).
            outcome = source_outcome(
                result,
                bool(attempt["auth_failed"]),
                bool(getattr(collector, "successful_empty_result", False)),
            )
            # Keep the last useful failure card if a later source fails silently.
            if outcome == "auth_failed":
                health_updates[candidate["source_id"]] = "auth_failed"
                last_kept_failure_result = result or last_kept_failure_result
                continue
            if outcome == "degraded":
                # An optional request or a refresh retry may return 401/403 even
                # though the collector produced usable quota. Keep the data;
                # preserve the partial failure for the source diagnostics.
                health_updates[candidate["source_id"]] = "degraded"
            if outcome == "unavailable":
                # Any failed attempt that did not trigger the auth_failed short-circuit
                # (e.g. missing_config, api_error, or empty response) is functionally down.
                # Note: Transient errors (rate_limited, timeout) trigger failover to try the
                # next candidate; rate-limit backoff is managed upstream by SmartCollector._mark_429.
                # The 'unavailable' status persists across polls until overwritten by the next
                # successful collection pass.
                health_updates[candidate["source_id"]] = "unavailable"
                last_kept_failure_result = result or last_kept_failure_result
                continue
            if not identity_verification and await self._cookie_switched_account(
                provider_id, account_id, candidate, collector
            ):
                # The browser now holds a different account than the one this source is tagged
                # to: the tag is gone and the source is pending again. Publish nothing from
                # this poll under the old account.
                continue
            if outcome == "healthy":
                health_updates[candidate["source_id"]] = "healthy"
            # A provider may learn a stable identity only after calling its
            # upstream API (Antigravity userinfo is one example). Bind that
            # identity to the exact source used for this successful attempt.
            # Never infer it from another account's historical usage.
            resolved_id = getattr(collector, "account_id", None)
            if (
                account_id == "default"
                and is_sidecar_source(candidate)
                and candidate.get("credential_origin")
                and isinstance(resolved_id, str)
                and resolved_id.strip()
                and resolved_id.strip().lower() != "default"
            ):
                if not await self._promote_source_identity(
                    provider_id,
                    account_id,
                    candidate["source_id"],
                    resolved_id,
                    cache_account_id=cache_slot,
                    **self._source_proof_kwargs(collector),
                ) and (
                    rejection_reason := self._promotion_rejection_reason(
                        provider_id, candidate, collector
                    )
                ):
                    successful_result = self._discard_stale_source_result(smart, rejection_reason)
                    continue
            elif (
                account_id == "default"
                and is_sidecar_source(candidate)
                and candidate.get("credential_origin")
            ):
                # Let an unresolved source call its API so it can prove its
                # identity, but never publish an unidentified quota card into
                # the shared default account history.
                await self._preview_unresolved_source(provider_id, candidate, result)
                # Keep going: a source that works but cannot name its account must not
                # starve the other pending sources of their own verification.
                successful_result = []
                continue
            if not await self._claude_organization_allows_publication(
                provider_id, collector, candidate
            ):
                successful_result = self._discard_stale_source_result(smart)
                continue
            successful_result = self._protect_claude_cards(
                provider_id, collector, candidate, result
            )
            break
        # Every source tried but not promoted (a promotion deletes its pending row, so
        # noting it is a no-op) is scheduled for a later retry.
        await self._note_unverified(identity_verification, provider_id, attempted)
        if successful_result is None and last_kept_failure_result is None and skipped_for_renewal:
            # Nothing ran, so the collector still holds the previous poll's state (often
            # "complete"). Say "skipped" so the poller keeps the last good cards instead of
            # reconciling them away, and the server-credential stamping leaves rows alone.
            smart._set_collection_state(
                "skipped",
                "login expired — "
                + await self._renewal_wait_reason(provider_id, renewal_candidate),
            )
        return (
            successful_result if successful_result is not None else (last_kept_failure_result or [])
        )
