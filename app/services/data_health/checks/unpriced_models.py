"""Classify token-bearing $0 usage with pricing and report evidence at each
event's timestamp. A positive recomputed cost is actionable; a resolved rate
that computes to $0 is verified by the configured price table; a source
reported $0 without a matching computed positive cost is informational, not
claimed as independently verified; and no rate/no report needs a seed row.

`:free`-suffixed models and the `opencode-free` provider are excluded
outright — priced at $0 on purpose, not missing a seed.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from app.models.db import ProviderConfig, UsageEvent
from app.services.cost_calculator import resolve_price_row
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
from app.services.maintenance.recost import apply_recost, plan_recost

_EXCLUDED_PROVIDERS = {"opencode-free"}


def _is_excluded(provider_id: str, model_id: str) -> bool:
    return provider_id in _EXCLUDED_PROVIDERS or model_id.endswith(":free")


def _classify_events(session: Session) -> dict[tuple[str, str], dict[str, int]]:
    configs = {
        (provider_id, account_id): billing_type
        for provider_id, account_id, billing_type in session.execute(
            select(
                ProviderConfig.provider_id, ProviderConfig.account_id, ProviderConfig.billing_type
            )
        ).all()
    }
    result: dict[tuple[str, str], dict[str, int]] = {}
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
            UsageEvent.cost_reported_usd,
        ).where(
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
        cost_reported_usd,
    ) in events:
        assert model_id is not None
        if _is_excluded(provider_id, model_id):
            continue
        key = (provider_id, model_id)
        buckets = result.setdefault(
            key,
            {"recost_fixes_it": 0, "verified_zero": 0, "source_reported": 0, "needs_seed_row": 0},
        )
        cache_key = (provider_id, model_id, ts.date())
        if cache_key not in price_rows:
            price_rows[cache_key] = resolve_price_row(session, provider_id, model_id, ts)
        price = price_rows[cache_key]
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
            billing_type=configs.get((provider_id, account_id), "unknown"),
            reported_cost=cost_reported_usd,
            resolved_price_row=price,
            price_row_resolved=True,
        )
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
        buckets[category] += 1
    return result


class UnpricedModelsCheck(Check):
    id = "unpriced_models"
    severity = Severity.WARN
    blocked_by = ("legacy_provider_ids",)

    def detect(self, session: Session) -> CheckReport:
        # Classification uses each event's effective pricing date. Grouping by
        # provider/model alone would mislabel older events when prices change.
        by_provider: dict[str, list[dict[str, Any]]] = {}
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

        groups: list[FindingGroup] = []
        has_actionable = False
        for provider_id, models in sorted(by_provider.items()):
            actionable = sum(m["recost_fixes_it"] + m["needs_seed_row"] for m in models)
            has_actionable = has_actionable or actionable > 0
            any_fixable = any(m["recost_fixes_it"] > 0 for m in models)
            informational = sum(m["verified_zero"] + m["source_reported"] for m in models)
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
                            else "no resolvable price row for any model under this provider "
                            "— add a PRICING_SEED row"
                        ),
                        samples=[
                            Finding(label=f"{provider_id}/{m['model_id']}", detail=m)
                            for m in models[:10]
                        ],
                        detail={"by_model": models},
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
                            for m in models[:10]
                        ],
                        detail={"by_model": models},
                    )
                )
        return CheckReport(
            check_id=self.id,
            severity=Severity.WARN if has_actionable else Severity.INFO,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
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
        provider_id = group_key
        result = apply_recost(session, [provider_id], only_zero_cost=True)
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
