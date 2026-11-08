"""Classify token-bearing $0 usage with pricing and report evidence at each
event's timestamp. A positive recomputed cost is actionable; a resolved rate
that computes to $0 is verified by the configured price table; a source
reported $0 without a matching computed positive cost is informational, not
claimed as independently verified; and no rate/no report needs a seed row.

Free-tier model ids (`:free`-, `-free`- and `/free`-suffixed, plus the
`openrouter/free/*` prefix form), sentinels (`unknown`, empty), and the
`opencode-free` provider are excluded outright — priced at $0 on purpose,
not missing a seed.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from app.models.db import ProviderConfig, UsageEvent
from app.services.cost_calculator import PricingIndex, is_aggregate_event_id, resolve_price_row
from app.services.data_health.base import (
    AsyncHook,
    Check,
    CheckReport,
    Finding,
    FindingGroup,
    FixPlan,
    FixResult,
    Severity,
)
from app.services.maintenance.event_cost import resolve_event_cost
from app.services.maintenance.recost import (
    apply_recost,
    is_priced_on_purpose_free,
    legacy_reported_cost,
    plan_recost,
)
from app.services.maintenance.retry import DATA_HEALTH_LOCK_PROFILE


def _is_excluded(provider_id: str, model_id: str) -> bool:
    # Free-tier ids are excluded for every provider, not just the OpenCode
    # ones: the cost calculator's segment-trim fallback is blocked for them
    # too, so a "-free" id can never reach a paid sibling row and be billed
    # at list price.
    return is_priced_on_purpose_free(provider_id, model_id)


class _Buckets(dict[str, int]):
    """The four public categories for one (provider_id, model_id) pair.

    A plain mapping of counter name to count, so that the memory benchmark's
    digest of `_classify_events` keeps matching a baseline it compares
    against (see `docs/server-memory.md`) — the extra fact below is an
    attribute, not a fifth counter.

    `seed_proof` counts the `needs_seed_row` events that a PRICING_SEED row
    would not help: the account is billed pay-as-you-go and has a reported
    cost of $0, so the report wins over any estimate and re-costing computes
    the same $0. The group reason needs that distinction — advising a seed
    row there would be a dead end.
    """

    seed_proof: int = 0


def _classify_events(session: Session) -> dict[tuple[str, str], _Buckets]:
    configs = {
        (provider_id, account_id): billing_type
        for provider_id, account_id, billing_type in session.execute(
            select(
                ProviderConfig.provider_id, ProviderConfig.account_id, ProviderConfig.billing_type
            )
        ).all()
    }
    result: dict[tuple[str, str], _Buckets] = {}
    pricing_index = PricingIndex.load(session)
    price_rows = {}
    events = session.execute(
        select(  # type: ignore[call-overload]
            UsageEvent.provider_id,
            UsageEvent.account_id,
            UsageEvent.model_id,
            UsageEvent.ts,
            UsageEvent.tokens_input,
            UsageEvent.tokens_output,
            UsageEvent.tokens_cache_read,
            UsageEvent.tokens_cache_create,
            UsageEvent.tokens_reasoning,
            UsageEvent.tokens_cache_create_1h,
            UsageEvent.tokens_cache_create_5m,
            UsageEvent.cost_usd,
            UsageEvent.cost_reported_usd,
            UsageEvent.event_id,
        )
        .where(
            col(UsageEvent.kind) == "message",
            col(UsageEvent.cost_usd) == 0.0,
            col(UsageEvent.model_id).is_not(None),
            (
                col(UsageEvent.tokens_input)
                + col(UsageEvent.tokens_output)
                + col(UsageEvent.tokens_cache_read)
                + col(UsageEvent.tokens_cache_create)
                + col(UsageEvent.tokens_reasoning)
            )
            > 0,
        )
        .execution_options(yield_per=1000)
    )
    for (
        provider_id,
        account_id,
        model_id,
        ts,
        tokens_input,
        tokens_output,
        tokens_cache_read,
        tokens_cache_create,
        tokens_reasoning,
        tokens_cache_create_1h,
        tokens_cache_create_5m,
        cost_usd,
        cost_reported_usd,
        event_id,
    ) in events:
        assert model_id is not None
        if _is_excluded(provider_id, model_id):
            continue
        key = (provider_id, model_id)
        buckets = result.get(key)
        if buckets is None:
            # Not `setdefault`: it evaluates its default eagerly, so every
            # event would allocate a throwaway `_Buckets`. The classify case
            # is what the memory benchmark tunes for (docs/server-memory.md).
            buckets = _Buckets(
                recost_fixes_it=0,
                verified_zero=0,
                source_reported=0,
                needs_seed_row=0,
            )
            result[key] = buckets
        cache_key = (provider_id, model_id, ts.date())
        if cache_key not in price_rows:
            price_rows[cache_key] = resolve_price_row(
                session, provider_id, model_id, ts, index=pricing_index
            )
        price = price_rows[cache_key]
        reported = legacy_reported_cost(cost_reported_usd, provider_id, cost_usd)
        billing_type = configs.get((provider_id, account_id), "unknown")
        resolved = resolve_event_cost(
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
            billing_type=billing_type,
            reported_cost=reported,
            standard_tier_only=is_aggregate_event_id(event_id),
            resolved_price_row=price,
            price_row_resolved=True,
        )
        # The scan selects events whose stored cost is zero. A positive
        # resolved cost (including a PAYG-reported amount) will therefore
        # increase the stored cost under apply_recost's only_zero_cost guard.
        if resolved.cost_usd > 0:
            category = "recost_fixes_it"
        elif price is not None and resolved.cost_estimated_usd == 0:
            category = "verified_zero"
        elif cost_reported_usd is not None:
            # An upstream $0 is observable evidence, not independent proof
            # of free usage; keep it informational and out of the warning total.
            category = "source_reported"
        else:
            category = "needs_seed_row"
            if reported == 0 and billing_type == "pay_as_you_go":
                # A $0 report on a pay-as-you-go account wins over any
                # estimate, so re-costing computes $0 again and a
                # PRICING_SEED row would not help. The group reason must
                # not send the operator after one.
                buckets.seed_proof += 1
        buckets[category] += 1
    return result


def _not_fixable_reason(seed_proof: int, unseeded: int) -> str:
    """Why a provider's zero-cost events cannot be recosted.

    Two different situations end up in `needs_seed_row`, and they need
    different advice. A model with no rate at all needs a PRICING_SEED row. A
    model on a pay-as-you-go account that reported $0 is not something a price
    row can change — the report wins over the estimate, so re-costing
    recomputes the same $0.
    """
    if not seed_proof:
        return "no resolvable price row for any model under this provider — add a PRICING_SEED row"
    if not unseeded:
        return (
            "the source reported $0 and this account is billed pay-as-you-go, so the "
            "report wins over the price estimate — re-costing will not change these events"
        )
    return (
        "no resolvable price row for some models under this provider — add a "
        "PRICING_SEED row; for the others the source reported $0 and this account "
        "is billed pay-as-you-go, so re-costing will not change them"
    )


class UnpricedModelsCheck(Check):
    id = "unpriced_models"
    title = "Some token usage has no reliable cost"
    description = "Token-bearing events have a zero stored cost. Some are fixable, while configured free rates or source-reported zeros may only need review."
    impact = "Usage totals may understate spend when no applicable price is available."
    recommended_action = "Review the per-model evidence. Recompute cost when a rate is available, or add a pricing seed for models with no rate."
    severity = Severity.WARN
    blocked_by = ("legacy_provider_ids",)

    def detect(self, session: Session) -> CheckReport:
        # Classification uses each event's effective pricing date. Grouping by
        # provider/model alone would mislabel older events when prices change.
        by_provider: dict[str, list[dict[str, Any]]] = {}
        # Per provider, how the zero-cost events split: those a $0 report on a
        # pay-as-you-go account keeps at $0 (no seed row would help), and those
        # with no rate at all (a seed row would). Only the group's reason text
        # uses these.
        seed_proof: dict[str, int] = {}
        unseeded: dict[str, int] = {}
        for (provider_id, model_id), counts in _classify_events(session).items():
            count = sum(counts.values())
            populated = [name for name, value in counts.items() if value > 0]
            by_provider.setdefault(provider_id, []).append(
                {
                    "model_id": model_id,
                    "count": count,
                    "classification": populated[0] if len(populated) == 1 else "mixed_evidence",
                    **counts,
                }
            )
            seed_proof[provider_id] = seed_proof.get(provider_id, 0) + counts.seed_proof
            unseeded[provider_id] = unseeded.get(provider_id, 0) + (
                counts["needs_seed_row"] - counts.seed_proof
            )

        groups: list[FindingGroup] = []
        has_actionable = False
        for provider_id, models in sorted(by_provider.items()):
            actionable = sum(m["recost_fixes_it"] + m["needs_seed_row"] for m in models)
            has_actionable = has_actionable or actionable > 0
            any_fixable = any(m["recost_fixes_it"] > 0 for m in models)
            informational = sum(m["verified_zero"] + m["source_reported"] for m in models)
            actionable_models = [
                {
                    **model,
                    "count": model["recost_fixes_it"] + model["needs_seed_row"],
                    "verified_zero": 0,
                    "source_reported": 0,
                }
                for model in models
                if model["recost_fixes_it"] + model["needs_seed_row"] > 0
            ]
            informational_models = [
                {
                    **model,
                    "count": model["verified_zero"] + model["source_reported"],
                    "recost_fixes_it": 0,
                    "needs_seed_row": 0,
                }
                for model in models
                if model["verified_zero"] + model["source_reported"] > 0
            ]
            if actionable:
                groups.append(
                    FindingGroup(
                        key=provider_id,
                        label=f"{provider_id}: {actionable} zero-cost event(s) need review",
                        count=actionable,
                        fixable=any_fixable,
                        not_fixable_reason=(
                            None
                            if any_fixable
                            else _not_fixable_reason(
                                seed_proof.get(provider_id, 0), unseeded.get(provider_id, 0)
                            )
                        ),
                        samples=[
                            Finding(label=f"{provider_id}/{m['model_id']}", detail=m)
                            for m in actionable_models[:10]
                        ],
                        detail={"by_model": actionable_models},
                    )
                )
            if informational:
                groups.append(
                    FindingGroup(
                        key=f"{provider_id}::informational-zero",
                        label=f"{provider_id}: {informational} zero-cost event(s) have price/source evidence",
                        count=informational,
                        fixable=False,
                        not_fixable_reason="Zero-rate price rows are configured as $0; source-reported $0 is informative, not independently verified.",
                        samples=[
                            Finding(label=f"{provider_id}/{m['model_id']}", detail=m)
                            for m in informational_models[:10]
                        ],
                        detail={"by_model": informational_models},
                    )
                )
        return CheckReport(
            check_id=self.id,
            severity=Severity.WARN if has_actionable else Severity.INFO,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        if group_key.endswith("::informational-zero"):
            raise ValueError("informational zero-cost findings cannot be recosted")
        provider_id = group_key
        recost_plan = plan_recost(session, [provider_id], only_zero_cost=True, sample_size=10)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=f"Recost {provider_id}'s zero-cost priced events",
            counts={
                "updated": recost_plan.updated,
                "unchanged": recost_plan.unchanged,
                "zeroed": recost_plan.zeroed,
                "skipped_still_unpriced": recost_plan.skipped_still_unpriced,
            },
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        if group_key.endswith("::informational-zero"):
            raise ValueError("informational zero-cost findings cannot be recosted")
        provider_id = group_key
        result = apply_recost(
            session, [provider_id], only_zero_cost=True, lock_profile=DATA_HEALTH_LOCK_PROFILE
        )
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Recosted {provider_id}'s zero-cost priced events",
                counts={
                    "updated": result.updated,
                    "unchanged": result.unchanged,
                    "zeroed": result.zeroed,
                    "skipped_still_unpriced": result.skipped_still_unpriced,
                    "rollups_rebuilt_pairs": result.rollups_rebuilt_pairs,
                    "windows_rebuilt": result.windows_rebuilt,
                },
            ),
            [],
        )
