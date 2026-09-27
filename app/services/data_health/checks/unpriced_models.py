"""Data Health `unpriced_models` check — token-bearing message events priced
at $0.00 (D8 in the v3.0.0 prod-cleanup audit: `gpt-6-luna`, `gpt-6-sol`,
bare `gpt-5.6`). Each (provider, model) group is classified by resolving a
price row at its earliest and latest event — `resolve_price_row` (not
`compute_event_cost_breakdown`) so a genuinely zero-rated seed row and no
seed row at all are told apart:

- a price row resolves at either end → `recost_fixes_it`: the fixer just
  hasn't run since the seed was added.
- no price row, but every event already carries a `cost_reported_usd` →
  `source_reported`: this is a real $0 (or the provider's own number),
  working as intended, not a bug.
- no price row and no report → `needs_seed_row`: not fixable here; add a
  `PRICING_SEED` row first.

`:free`-suffixed models and the `opencode-free` provider are excluded
outright — priced at $0 on purpose, not missing a seed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.db import UsageEvent
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
from app.services.maintenance.recost import apply_recost, plan_recost

_EXCLUDED_PROVIDERS = {"opencode-free"}


def _is_excluded(provider_id: str, model_id: str) -> bool:
    return provider_id in _EXCLUDED_PROVIDERS or model_id.endswith(":free")


def _classify(session: Session, provider_id: str, model_id: str, count: int, ts_min, ts_max) -> str:
    if resolve_price_row(session, provider_id, model_id, ts_min) is not None:
        return "recost_fixes_it"
    if resolve_price_row(session, provider_id, model_id, ts_max) is not None:
        return "recost_fixes_it"
    reported = session.execute(
        select(func.count())
        .select_from(UsageEvent)
        .where(
            col(UsageEvent.provider_id) == provider_id,
            col(UsageEvent.model_id) == model_id,
            col(UsageEvent.kind) == "message",
            col(UsageEvent.cost_usd) == 0.0,
            col(UsageEvent.cost_reported_usd).is_not(None),
        )
    ).scalar_one()
    return "source_reported" if reported == count else "needs_seed_row"


class UnpricedModelsCheck(Check):
    id = "unpriced_models"
    severity = Severity.WARN
    blocked_by = ("legacy_provider_ids",)

    def detect(self, session: Session) -> CheckReport:
        # 5 columns exceeds SQLModel's typed select() overloads.
        rows = session.execute(
            select(  # type: ignore[call-overload]
                UsageEvent.provider_id,
                UsageEvent.model_id,
                func.count(),
                func.min(UsageEvent.ts),
                func.max(UsageEvent.ts),
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
                )
                > 0,
            )
            .group_by(UsageEvent.provider_id, UsageEvent.model_id)
        ).all()

        by_provider: dict[str, list[dict[str, Any]]] = {}
        for provider_id, model_id, count, ts_min, ts_max in rows:
            if _is_excluded(provider_id, model_id):
                continue
            classification = _classify(session, provider_id, model_id, count, ts_min, ts_max)
            by_provider.setdefault(provider_id, []).append(
                {"model_id": model_id, "count": count, "classification": classification}
            )

        groups: list[FindingGroup] = []
        for provider_id, models in sorted(by_provider.items()):
            total = sum(m["count"] for m in models)
            any_fixable = any(m["classification"] == "recost_fixes_it" for m in models)
            groups.append(
                FindingGroup(
                    key=provider_id,
                    label=f"{provider_id}: {total} zero-cost priced event(s)",
                    count=total,
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
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
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
