"""Data Health `rollup_drift` check — the `usage_period_rollup` lifetime
all-grain row (`model_id=""`, `sidecar_id=""`) no longer matches a straight
`usage_events` aggregate for the same `(provider_id, account_id)`. The
incremental updater (`EventIngestor`) should keep these in lock-step; drift
means a repair elsewhere touched `usage_events` directly without rebuilding
rollups, or an older bug left a gap. Fix: `rebuild_rollups_for_pairs`, a
full recompute for that pair — cheap and unconditionally correct, unlike
trying to diagnose which increment went missing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.db import UsageEvent, UsagePeriodRollup
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
from app.services.maintenance.rollups import rebuild_rollups_for_pairs

_COST_EPSILON = 0.01
_KEY_SEP = "::"


def _key(provider_id: str, account_id: str) -> str:
    return f"{provider_id}{_KEY_SEP}{account_id}"


def _parse_key(group_key: str) -> tuple[str, str]:
    provider_id, _, account_id = group_key.partition(_KEY_SEP)
    return provider_id, account_id


def _actual_totals(session: Session, provider_id: str, account_id: str) -> tuple[int, float]:
    count, cost = session.execute(
        select(func.count(), func.coalesce(func.sum(UsageEvent.cost_usd), 0.0)).where(
            col(UsageEvent.provider_id) == provider_id,
            col(UsageEvent.account_id) == account_id,
            col(UsageEvent.kind) == "message",
        )
    ).one()
    return count, cost


def _rollup_totals(session: Session, provider_id: str, account_id: str) -> tuple[int, float]:
    row = session.exec(
        select(UsagePeriodRollup).where(
            col(UsagePeriodRollup.provider_id) == provider_id,
            col(UsagePeriodRollup.account_id) == account_id,
            col(UsagePeriodRollup.period_type) == "lifetime",
            col(UsagePeriodRollup.period_key) == "all",
            col(UsagePeriodRollup.model_id) == "",
            col(UsagePeriodRollup.sidecar_id) == "",
        )
    ).first()
    if row is None:
        return 0, 0.0
    return row.msgs, row.cost_usd


class RollupDriftCheck(Check):
    id = "rollup_drift"
    severity = Severity.WARN
    blocked_by = ("legacy_provider_ids",)

    def detect(self, session: Session) -> CheckReport:
        pairs = {
            (row[0], row[1])
            for row in session.execute(
                select(UsageEvent.provider_id, UsageEvent.account_id)
                .distinct()
                .where(col(UsageEvent.kind) == "message")
            )
        }
        groups: list[FindingGroup] = []
        for provider_id, account_id in sorted(pairs):
            actual_msgs, actual_cost = _actual_totals(session, provider_id, account_id)
            rollup_msgs, rollup_cost = _rollup_totals(session, provider_id, account_id)
            if actual_msgs == rollup_msgs and abs(actual_cost - rollup_cost) <= _COST_EPSILON:
                continue
            detail = {
                "actual_msgs": actual_msgs,
                "rollup_msgs": rollup_msgs,
                "actual_cost_usd": round(actual_cost, 4),
                "rollup_cost_usd": round(rollup_cost, 4),
            }
            groups.append(
                FindingGroup(
                    key=_key(provider_id, account_id),
                    label=f"{provider_id}/{account_id}: rollup drift",
                    count=abs(actual_msgs - rollup_msgs) or 1,
                    fixable=True,
                    samples=[Finding(label=f"{provider_id}/{account_id}", detail=detail)],
                    detail=detail,
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        provider_id, account_id = _parse_key(group_key)
        actual_msgs, actual_cost = _actual_totals(session, provider_id, account_id)
        rollup_msgs, rollup_cost = _rollup_totals(session, provider_id, account_id)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=f"Rebuild rollups for {provider_id}/{account_id}",
            counts={
                "actual_msgs": actual_msgs,
                "rollup_msgs_before": rollup_msgs,
                "actual_cost_usd": round(actual_cost, 4),
                "rollup_cost_usd_before": round(rollup_cost, 4),
            },
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        provider_id, account_id = _parse_key(group_key)
        rebuild_rollups_for_pairs(session, {(provider_id, account_id)})
        session.commit()
        actual_msgs, actual_cost = _actual_totals(session, provider_id, account_id)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Rebuilt rollups for {provider_id}/{account_id}",
                counts={
                    "msgs_after": actual_msgs,
                    "cost_usd_after": round(actual_cost, 4),
                },
            ),
            [],
        )
