"""Try each credential source of one account once, live, and write nothing (#434).

Failover only ever shows the source that won. This answers "which of my credentials work
right now, and why does the others fail" on demand — without the side effects a real
collection has: no health/attempt provenance, no promotion or pending preview, no cookie-tag
revocation, no verification backoff, and no token refresh. Each source runs on its own fresh
collector instance (never the poller's, which a probe would otherwise race), pinned to that
one source with ``token_cache.using_source``.

What a probe cannot undo: it is a real request, so the provider sees it, and a collector's own
error handling may log a provider error event. The account's in-memory "rejected" flag is
restored afterwards so a probe never turns a status page red by itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from app.core.log_redaction import redact_secrets
from app.core.utils import scrub_log
from app.services import auth_failures
from app.services.token_cache import token_cache

if TYPE_CHECKING:
    from app.services.collector_manager import CollectorManager

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 20.0
_AUTH_ERROR_TYPES = {"auth_failed", "invalid_api_key"}


def has_usable_card(result: list[dict[str, Any]]) -> bool:
    return any(
        card.get("data_source") != "error"
        and card.get("remaining") != "ERR"
        and not card.get("error_type")
        for card in result
    )


def source_outcome(result: list[dict[str, Any]], auth_rejected: bool, empty_allowed: bool) -> str:
    """Classify one source attempt: ``healthy`` | ``degraded`` | ``auth_failed`` | ``unavailable``.

    The one rule behind both failover's health bookkeeping and the on-demand probe. A fetch
    that still produced usable cards is not failed (an optional request or a refresh retry may
    401 while quota is collected: ``degraded``); a failed one is ``auth_failed`` when the
    provider rejected the credential, else ``unavailable``.
    """
    failed = (not result and not empty_allowed) or bool(result and not has_usable_card(result))
    rejected = auth_rejected or any(card.get("error_type") in _AUTH_ERROR_TYPES for card in result)
    if failed:
        return "auth_failed" if rejected else "unavailable"
    return "degraded" if auth_rejected else "healthy"


def isolated_collector(template: Any) -> Any:
    """A fresh collector configured like *template* that shares no state with it."""
    clone = type(template)(
        account_id=getattr(template, "account_id", None),
        account_label=getattr(template, "account_label", None),
    )
    if hasattr(template, "credential_account_id"):
        clone.credential_account_id = template.credential_account_id
    user_strategies = getattr(template, "_user_strategies", None)
    if user_strategies:
        clone.apply_strategy_config(user_strategies)
    return clone


def _find_template(manager: CollectorManager, provider_id: str, account_id: str) -> Any:
    live = manager.smart_collectors.get(f"{provider_id}:{account_id}")
    if live is None:
        live = next(
            (
                sc
                for key, sc in manager.smart_collectors.items()
                if key.startswith(f"{provider_id}:")
                and getattr(sc.collector, "account_id", None) == account_id
            ),
            None,
        )
    return live.collector if live is not None else manager._create_collector(provider_id)


def _skipped(candidate: dict[str, Any], outcome: str) -> dict[str, Any]:
    return {"source_id": candidate["source_id"], "outcome": outcome, "probed": False}


async def _probe_one(
    manager: CollectorManager,
    template: Any,
    provider_id: str,
    slot: str,
    candidate: dict[str, Any],
    all_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if manager._awaiting_machine_renewal(provider_id, candidate, all_candidates):
        # A rotating login its machine's CLI owns: calling with an expired token can only 401,
        # and any refresh here would sign that CLI out.
        return _skipped(candidate, "waiting_on_machine")

    collector = isolated_collector(template)
    # No server-side refresh from a probe: a refresh would rotate a refresh token (or write a
    # fresh one into the cache) as a side effect of "just looking".
    collector.REFRESHABLE = False
    manager._reset_attempt_identity(collector, slot, getattr(template, "account_label", None))

    last_status: int | None = None

    async def note_response(response: httpx.Response) -> None:
        nonlocal last_status
        last_status = response.status_code

    started = time.monotonic()
    result: list[dict[str, Any]] = []
    error: dict[str, str] | None = None
    cache_slot = candidate.get("account_slot") or slot
    async with token_cache.using_source(provider_id, cache_slot, candidate["source_id"]) as attempt:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_SECONDS,
            event_hooks={"response": [token_cache.observe_response, note_response]},
        ) as client:
            try:
                result = await asyncio.wait_for(
                    collector.collect(client), timeout=PROBE_TIMEOUT_SECONDS + 5
                )
            except Exception as exc:
                logger.debug(
                    "Source probe failed for %s/%s (%s): %s",
                    scrub_log(provider_id),
                    scrub_log(slot),
                    scrub_log(candidate["source_id"]),
                    scrub_log(str(exc)),
                )
                error = {"type": type(exc).__name__, "message": str(redact_secrets(str(exc)))[:300]}
        rejected = bool(attempt["auth_failed"])

    empty_allowed = bool(getattr(collector, "successful_empty_result", False))
    outcome = "unavailable" if error else source_outcome(result, rejected, empty_allowed)
    card_error = next((c.get("error_type") for c in result if c.get("error_type")), None)
    return {
        "source_id": candidate["source_id"],
        "outcome": outcome,
        "probed": True,
        "http_status": last_status,
        "error_type": (error or {}).get("type") or card_error,
        "message": (error or {}).get("message"),
        "cards": sum(1 for card in result if not card.get("error_type")),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


async def probe_sources(
    manager: CollectorManager, provider_id: str, account_id: str
) -> list[dict[str, Any]]:
    """Probe every credential source of ``(provider_id, account_id)``, in failover order."""
    await manager._sync_collectors()
    template = _find_template(manager, provider_id, account_id)
    if template is None:
        return []
    slot = (
        getattr(template, "credential_account_id", None)
        or getattr(template, "account_id", None)
        or account_id
        or "default"
    )
    everything = await manager._source_candidates(provider_id, slot, False)
    pending = [c for c in everything if c.get("identity_pending")]
    live = [c for c in everything if not c.get("identity_pending")]
    ordered = manager._ordered_candidates(provider_id, slot, live, include_resting=True)
    ordered_ids = {c["source_id"] for c in ordered}
    disabled = [c for c in live if c["source_id"] not in ordered_ids]

    flagged_before = slot in auth_failures.flagged_accounts(provider_id)
    results: list[dict[str, Any]] = []
    try:
        for candidate in ordered:
            results.append(await _probe_one(manager, template, provider_id, slot, candidate, live))
    finally:
        if not flagged_before:
            auth_failures.clear(provider_id, slot)
    results.extend(_skipped(c, "disabled") for c in disabled)
    results.extend(_skipped(c, "pending") for c in pending)
    return results
