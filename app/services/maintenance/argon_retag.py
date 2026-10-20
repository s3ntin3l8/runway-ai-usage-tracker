"""Canonicalise already-ingested Gemini 4 Argon model ids — issue #686.

The Antigravity extractor reported the raw model id as ``gemini-4-argon-trial``
(the ``-trial`` suffix is part of the id, not a version qualifier) and the
Gemini/Hermes normalizer passed it through verbatim, so ``usage_events`` rows
carry ``gemini-4-argon-trial`` while the seeded pricing rows are keyed on the
canonical ``gemini-4-argon`` (see ``app/services/pricing_seed.py``).

Both normalizers now canonicalise the Argon family
(``app/services/model_normalization.py``), so newly ingested events store
``gemini-4-argon``. This module repairs the rows ingested before that: the
model id is rewritten to the canonical id and the event is re-priced from
``provider_pricing`` with the same rule ingest uses
(``event_cost.resolve_event_cost``), so an event that sat at $0.00 because its
id matched no row picks up the Argon rate.

``model_id`` is part of the ``usage_period_rollup`` grain and of the
``usage_windows`` series identity, so the rollups and windows of every touched
(provider_id, account_id) pair are rebuilt afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import update
from sqlmodel import Session, col, select

from app.models.db import ProviderConfig, UsageEvent
from app.services.cost_calculator import PricingIndex, resolve_price_row
from app.services.maintenance.event_cost import ResolvedCost, resolve_event_cost
from app.services.maintenance.windows import rebuild_windows_for_providers
from app.services.model_normalization import ARGON_MODEL_ID, is_argon_model_id
from app.services.period_rollups import rebuild_rollups_for_pairs

# The providers whose pricing rows key Gemini 4 Argon on the canonical id
# (see pricing_seed.py). "hermes" never appears here: Hermes files Gemini
# traffic under the `gemini` provider.
ARGON_PROVIDER_IDS: tuple[str, ...] = ("antigravity", "gemini")

_BATCH = 500


@dataclass(frozen=True)
class ArgonRetagChange:
    """One event the repair would rewrite."""

    id: int
    provider_id: str
    account_id: str
    event_ref: str  # event_id column, for display
    old_model_id: str
    old_cost_usd: float
    resolved: ResolvedCost


@dataclass
class ArgonRetagPlan:
    total: int = 0
    repriced: int = 0  # events whose cost actually changes
    unpriced: int = 0  # events with no Argon pricing row at their timestamp
    by_model: dict[str, int] = field(default_factory=dict)
    cost_delta_usd: float = 0.0
    samples: list[ArgonRetagChange] = field(default_factory=list)


@dataclass
class ArgonRetagResult:
    retagged: int = 0
    repriced: int = 0
    unpriced: int = 0
    cost_delta_usd: float = 0.0
    rollups_rebuilt_pairs: int = 0
    windows_rebuilt: int = 0


def _provider_scope(providers: list[str] | None) -> tuple[str, ...]:
    return tuple(providers) if providers else ARGON_PROVIDER_IDS


def _affected_events(session: Session, providers: list[str] | None) -> list[UsageEvent]:
    """Events whose stored model id is an Argon spelling other than the canonical one.

    The SQL filter is deliberately loose (a prefix match); the precise family
    test runs in Python so a look-alike id such as "gemini-4-argonoid" is
    never rewritten.
    """
    stmt = select(UsageEvent).where(
        col(UsageEvent.kind) == "message",
        col(UsageEvent.provider_id).in_(_provider_scope(providers)),
        col(UsageEvent.model_id).like(f"{ARGON_MODEL_ID}%"),
    )
    events = list(session.exec(stmt).all())
    return [
        ev
        for ev in events
        if ev.model_id and ev.model_id != ARGON_MODEL_ID and is_argon_model_id(ev.model_id)
    ]


def _changes(session: Session, providers: list[str] | None) -> list[ArgonRetagChange]:
    """Resolve what every affected event would be rewritten to (read-only)."""
    configs = {
        (c.provider_id, c.account_id): c.billing_type
        for c in session.exec(select(ProviderConfig)).all()
    }
    pricing_index = PricingIndex.load(session)
    changes: list[ArgonRetagChange] = []
    for ev in _affected_events(session, providers):
        if ev.id is None:  # type-system-only guard: a persisted row always has an id
            continue
        price_row = resolve_price_row(
            session, ev.provider_id, ARGON_MODEL_ID, ev.ts, index=pricing_index
        )
        resolved = resolve_event_cost(
            session,
            provider_id=ev.provider_id,
            model_id=ARGON_MODEL_ID,
            ts=ev.ts,
            tokens_input=ev.tokens_input,
            tokens_output=ev.tokens_output,
            tokens_cache_read=ev.tokens_cache_read,
            tokens_cache_create=ev.tokens_cache_create,
            tokens_reasoning=ev.tokens_reasoning,
            tokens_cache_create_1h=ev.tokens_cache_create_1h,
            tokens_cache_create_5m=ev.tokens_cache_create_5m,
            billing_type=configs.get((ev.provider_id, ev.account_id), "unknown"),
            reported_cost=ev.cost_reported_usd,
            resolved_price_row=price_row,
            price_row_resolved=True,
        )
        changes.append(
            ArgonRetagChange(
                id=ev.id,
                provider_id=ev.provider_id,
                account_id=ev.account_id,
                event_ref=ev.event_id,
                old_model_id=ev.model_id or "",
                old_cost_usd=ev.cost_usd,
                resolved=resolved,
            )
        )
    return changes


def plan_argon_retag(
    session: Session, providers: list[str] | None = None, *, sample_size: int = 5
) -> ArgonRetagPlan:
    """Read-only preview of the repair — no writes, no commit."""
    plan = ArgonRetagPlan()
    for change in _changes(session, providers):
        plan.total += 1
        plan.by_model[change.old_model_id] = plan.by_model.get(change.old_model_id, 0) + 1
        if change.resolved.cost_usd == 0.0 and change.old_cost_usd == 0.0:
            plan.unpriced += 1
        else:
            plan.repriced += 1
        plan.cost_delta_usd += change.resolved.cost_usd - change.old_cost_usd
        if len(plan.samples) < sample_size:
            plan.samples.append(change)
    return plan


def apply_argon_retag(
    session: Session,
    providers: list[str] | None = None,
    *,
    skip_rollups: bool = False,
    skip_windows: bool = False,
) -> ArgonRetagResult:
    """Rewrite the affected events' model id and re-price them from provider_pricing.

    Every write is chunked and committed per batch, so the repair never holds
    the writer lock for one giant transaction; an interrupted run can simply be
    repeated. The rollups and windows of the touched (provider_id, account_id)
    pairs are rebuilt from events afterwards.
    """
    changes = _changes(session, providers)
    result = ArgonRetagResult()
    pairs: set[tuple[str, str]] = set()
    writes: list[dict[str, object]] = []

    for change in changes:
        resolved = change.resolved
        writes.append(
            {
                "id": change.id,
                "model_id": ARGON_MODEL_ID,
                "cost_usd": resolved.cost_usd,
                "cost_reported_usd": resolved.cost_reported_usd,
                "cost_estimated_usd": resolved.cost_estimated_usd,
                "cost_input": resolved.breakdown.input,
                "cost_output": resolved.breakdown.output,
                "cost_cache_read": resolved.breakdown.cache_read,
                "cost_cache_create": resolved.breakdown.cache_create,
            }
        )
        pairs.add((change.provider_id, change.account_id))
        result.retagged += 1
        if resolved.cost_usd == 0.0 and change.old_cost_usd == 0.0:
            result.unpriced += 1
        else:
            result.repriced += 1
        result.cost_delta_usd += resolved.cost_usd - change.old_cost_usd
        if len(writes) == _BATCH:
            session.execute(update(UsageEvent), writes)
            session.commit()
            writes.clear()
    if writes:
        session.execute(update(UsageEvent), writes)
    session.commit()

    if not skip_rollups and pairs:
        rebuild_rollups_for_pairs(session, pairs)
        session.commit()
        result.rollups_rebuilt_pairs = len(pairs)

    if not skip_windows and pairs:
        touched_providers = sorted({p for p, _a in pairs})
        result.windows_rebuilt = rebuild_windows_for_providers(session, touched_providers)

    return result
