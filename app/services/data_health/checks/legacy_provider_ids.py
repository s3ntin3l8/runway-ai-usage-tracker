"""Data Health `legacy_provider_ids` check — events stuck under an OpenCode-
sibling provider id Runway now folds into a canonical provider (D2/D3 in the
v3.0.0 prod-cleanup audit: `opencode-xai`, `opencode-openrouter`). See
`app/services/maintenance/legacy_providers.py` for the map and
`legacy_retag.py` for the fixer, including the collision-count semantics
`plan_legacy_retag` already computes.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from app.models.db import UsageEvent
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
from app.services.maintenance.legacy_providers import LEGACY_PROVIDER_MAP
from app.services.maintenance.legacy_retag import apply_legacy_retag, plan_legacy_retag


class LegacyProviderIdsCheck(Check):
    id = "legacy_provider_ids"
    title = "Usage uses an old provider ID"
    description = "Some usage events are stored under a provider name that Runway has since replaced with a canonical provider ID."
    impact = (
        "Usage can be split between provider names, which affects totals, pricing, and rollups."
    )
    recommended_action = (
        "Retag the affected events to the canonical provider shown in the finding preview."
    )
    severity = Severity.ERROR

    def detect(self, session: Session) -> CheckReport:
        present = {
            row[0]
            for row in session.execute(
                select(UsageEvent.provider_id)
                .distinct()
                .where(col(UsageEvent.provider_id).in_(LEGACY_PROVIDER_MAP.keys()))
            )
        }
        groups: list[FindingGroup] = []
        for legacy_id in sorted(present):
            retag_plan = plan_legacy_retag(session, legacy_id, sample_size=5)
            groups.append(
                FindingGroup(
                    key=legacy_id,
                    label=f"{legacy_id} → {retag_plan.canonical_provider_id}",
                    count=retag_plan.total,
                    fixable=True,
                    samples=[
                        Finding(label=event_id, detail={"event_id": event_id})
                        for event_id in retag_plan.samples
                    ],
                    detail={
                        "canonical_provider_id": retag_plan.canonical_provider_id,
                        "collisions": retag_plan.collisions,
                    },
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        retag_plan = plan_legacy_retag(session, group_key)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=f"Retag {group_key} onto {retag_plan.canonical_provider_id}",
            counts={
                "total": retag_plan.total,
                "collisions": retag_plan.collisions,
                "retagged": retag_plan.retagged,
            },
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        result = apply_legacy_retag(session, group_key)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Retagged {group_key} onto {result.canonical_provider_id}",
                counts={
                    "retagged": result.retagged,
                    "collisions_resolved": result.collisions_resolved,
                    "latest_usage_dropped": result.latest_usage_dropped,
                    "quota_snapshots_dropped": result.quota_snapshots_dropped,
                    "rollups_rebuilt_pairs": result.rollups_rebuilt_pairs,
                    "windows_rebuilt": result.windows_rebuilt,
                },
            ),
            [],
        )
