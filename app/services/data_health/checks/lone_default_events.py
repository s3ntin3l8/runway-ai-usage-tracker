"""Data Health check — usage and quota history still sitting under the
generic `default` account_id after a specific provider account exists.

Target validation is re-derived from configured accounts and stable account
identities in latest usage on every `plan`/`apply` call. Applying the explicit
preview moves usage events, quota cards, quota snapshots, and their source
contributions, then rebuilds event rollups/windows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.db import LatestUsage, QuotaSnapshot, UsageEvent
from app.services.data_health._provider_accounts import candidate_targets
from app.services.data_health.base import (
    AsyncHook,
    Check,
    CheckReport,
    Finding,
    FindingGroup,
    FixPlan,
    FixResult,
    ParamSpec,
    Severity,
)
from app.services.maintenance.account_merge import merge_gauge_series, plan_merge_gauge_series
from app.services.maintenance.event_reassign import apply_reassign_default, plan_reassign_default


class LoneDefaultEventsCheck(Check):
    id = "lone_default_events"
    title = "Usage is assigned to a generic account"
    description = "Usage events or quota history remain under “default” despite a specific account identity being available."
    impact = "Usage may be missing from the account that actually generated it."
    recommended_action = "Preview and move default history to the correct account. Choose a target when more than one identity exists."
    severity = Severity.ERROR
    blocked_by = ("config_default_keyed",)

    def detect(self, session: Session) -> CheckReport:
        counts = session.execute(
            select(UsageEvent.provider_id, UsageEvent.kind, func.count())
            .where(col(UsageEvent.account_id) == "default")
            .group_by(UsageEvent.provider_id, UsageEvent.kind)
        ).all()
        by_provider: dict[str, dict[str, int]] = {}
        for provider_id, kind, n in counts:
            by_provider.setdefault(provider_id, {})[kind] = n

        for provider_id, n in session.execute(
            select(LatestUsage.provider_id, func.count())
            .where(col(LatestUsage.account_id) == "default")
            .group_by(LatestUsage.provider_id)
        ).all():
            by_provider.setdefault(provider_id, {})["quota_cards"] = n
        for provider_id, n in session.execute(
            select(QuotaSnapshot.provider_id, func.count())
            .where(col(QuotaSnapshot.account_id) == "default")
            .group_by(QuotaSnapshot.provider_id)
        ).all():
            by_provider.setdefault(provider_id, {})["quota_history"] = n

        groups: list[FindingGroup] = []
        for provider_id, kinds in sorted(by_provider.items()):
            total = sum(kinds.values())
            candidates = candidate_targets(session, provider_id)
            sample = Finding(
                label=provider_id, detail={"provider_id": provider_id, "by_kind": kinds}
            )
            if len(candidates) == 1:
                groups.append(
                    FindingGroup(
                        key=provider_id,
                        label=f"{provider_id}: default → {candidates[0]}",
                        count=total,
                        fixable=True,
                        params=[
                            ParamSpec(name="target", label="Target account", options=candidates)
                        ],
                        samples=[sample],
                        detail={"by_kind": kinds, "suggested_target": candidates[0]},
                    )
                )
            else:
                reason = (
                    "no known non-default account for this provider"
                    if not candidates
                    else f"multiple candidate accounts, pick one: {', '.join(candidates)}"
                )
                groups.append(
                    FindingGroup(
                        key=provider_id,
                        label=f"{provider_id}: {total} event(s) under default",
                        count=total,
                        fixable=bool(candidates),
                        not_fixable_reason=None if candidates else reason,
                        params=(
                            [ParamSpec(name="target", label="Target account", options=candidates)]
                            if candidates
                            else []
                        ),
                        samples=[sample],
                        detail={"by_kind": kinds, "candidates": candidates},
                    )
                )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def _resolve_target(self, session: Session, provider_id: str, params: dict[str, Any]) -> str:
        candidates = candidate_targets(session, provider_id)
        target = params.get("target")
        if not target:
            if len(candidates) == 1:
                target = candidates[0]
            else:
                raise ValueError(
                    f"{provider_id!r} has no unambiguous target account; "
                    f"specify one of {candidates}"
                )
        target = str(target)
        if target == "default":
            raise ValueError("target account cannot be 'default'")
        if target not in candidates:
            raise ValueError(
                f"{target!r} is not a known account for {provider_id!r}; choose one of {candidates}"
            )
        return target

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        provider_id = group_key
        target = self._resolve_target(session, provider_id, params)
        reassign_plan = plan_reassign_default(
            session, provider_id=provider_id, source="default", target=target
        )
        gauge_plan = plan_merge_gauge_series(
            session, provider_id=provider_id, source="default", target=target
        )
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=f"Move {provider_id}/default history onto {target}",
            counts={
                "usage_events": reassign_plan.count,
                "quota_cards_merged": gauge_plan.merged,
                "quota_cards_retagged": gauge_plan.retagged,
                "quota_snapshots_retagged": gauge_plan.snapshots_retagged,
                "quota_snapshots_collided": gauge_plan.snapshots_collided,
            },
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        provider_id = group_key
        target = self._resolve_target(session, provider_id, params)
        result = apply_reassign_default(
            session, provider_id=provider_id, source="default", target=target
        )
        gauge_result = merge_gauge_series(
            session, provider_id=provider_id, source="default", target=target
        )
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Moved {provider_id}/default history onto {target}",
                counts={
                    "usage_events_moved": result.moved,
                    "quota_cards_merged": gauge_result.merged,
                    "quota_cards_retagged": gauge_result.retagged,
                    "quota_snapshots_retagged": gauge_result.snapshots_retagged,
                    "quota_snapshots_collided": gauge_result.snapshots_collided,
                    "rollups_rebuilt_pairs": result.rollups_rebuilt_pairs,
                    "windows_rebuilt": result.windows_rebuilt,
                },
            ),
            [],
        )
