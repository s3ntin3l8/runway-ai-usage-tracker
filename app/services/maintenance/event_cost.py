"""Single source of truth for turning event tokens into a cost_usd, used by
both live ingest (`EventIngestor`) and the offline recost tool
(`scripts/recost_events.py` / the Data Health `unpriced_models` fix).

Before this module, the two call sites disagreed in two ways:

1. **The Anthropic 1h/5m cache-create split.** Ingest always passed
   `tokens_cache_create_1h`/`_5m` into `compute_event_cost_breakdown`;
   `recost_events.py`'s Phase B never did, so re-running recost on an
   already-split event silently re-priced its cache-create tokens as if the
   split were unknown (all-5m-rate), overwriting a previously-correct split
   price with a wrong one.
2. **Unknown billing type.** Ingest used the computed estimate
   (`breakdown.total`) for every `billing_type="unknown"` account regardless
   of provider. Recost instead special-cased "unknown AND provider_id starts
   with 'opencode'" to keep the event's existing `cost_usd` unchanged — a
   narrow, provider-specific patch for OpenCode's legacy logged-subscription-
   amount events. `resolve_event_cost` replaces that patch with one general
   rule that helps every provider, not just OpenCode: when no pricing row
   resolves at all (see `resolve_price_row`) and a reported cost is
   available, trust the report instead of silently billing $0.00. A
   genuinely free/zero-rated model (a seeded row whose rates are 0) is
   unaffected — that's "a row resolved," not "no row resolved."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session

from app.models.db import ProviderPricing
from app.services.cost_calculator import (
    CostBreakdown,
    compute_event_cost_breakdown,
    resolve_price_row,
)


@dataclass(frozen=True)
class ResolvedCost:
    cost_usd: float
    cost_reported_usd: float | None
    cost_estimated_usd: float
    breakdown: CostBreakdown


def resolve_event_cost(  # noqa: PLR0913 — one param per priced token dimension / billing input
    session: Session,
    *,
    provider_id: str,
    model_id: str | None,
    ts: datetime,
    tokens_input: int,
    tokens_output: int,
    tokens_cache_read: int,
    tokens_cache_create: int,
    tokens_reasoning: int = 0,
    tokens_cache_create_1h: int = 0,
    tokens_cache_create_5m: int = 0,
    billing_type: str,
    reported_cost: float | None,
    resolved_price_row: ProviderPricing | None = None,
    price_row_resolved: bool = False,
) -> ResolvedCost:
    """Resolve the authoritative `cost_usd` for one event.

    `reported_cost` is whatever the caller considers the event's own
    reported/logged amount (ingest: the push's `cost_usd`; recost: the
    stored `cost_reported_usd`, or a caller-supplied legacy fallback for
    events that predate that column). This function only decides which of
    `reported_cost` / the computed estimate wins — deriving `reported_cost`
    itself from an event shape is the caller's job.

    Decision order:
    1. `pay_as_you_go` with a reported cost -> trust the report (the
       account's actual balance draw, not an estimate against a price list
       that may not match the provider's real metering).
    2. No pricing row resolves at all, but a reported cost is available ->
       trust the report rather than bill $0.00 for an unseeded model.
    3. Otherwise -> the computed estimate (`breakdown.total`).
    """
    price_row = (
        resolved_price_row
        if price_row_resolved
        else resolve_price_row(session, provider_id, model_id, ts)
    )
    breakdown = compute_event_cost_breakdown(
        session,
        provider_id=provider_id,
        model_id=model_id,
        ts=ts,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_create=tokens_cache_create,
        tokens_reasoning=tokens_reasoning,
        tokens_cache_create_1h=tokens_cache_create_1h,
        tokens_cache_create_5m=tokens_cache_create_5m,
        _resolved_price_row=price_row,
        _price_row_resolved=True,
    )

    cost_usd = breakdown.total
    if reported_cost is not None and (billing_type == "pay_as_you_go" or price_row is None):
        cost_usd = reported_cost

    return ResolvedCost(
        cost_usd=cost_usd,
        cost_reported_usd=reported_cost,
        cost_estimated_usd=breakdown.total,
        breakdown=breakdown,
    )
