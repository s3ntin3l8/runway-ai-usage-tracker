"""Data Health `cloud_coarse_events`: usage written by the first Claude cloud-runner collector.

That collector wrote one coarse event per sweep (no project, one model per session); its replacement
rebuilds the same usage from each session's history and refuses to run while the coarse events exist,
so they are removed first. See `app/services/maintenance/cloud_coarse_events.py` and
`docs/claude-cloud-event-contract.md`.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

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
from app.services.maintenance.cloud_coarse_events import (
    apply_cloud_coarse_events,
    plan_cloud_coarse_events,
)

GROUP_KEY = "coarse"


class CloudCoarseEventsCheck(Check):
    id = "cloud_coarse_events"
    title = "Claude cloud usage from the first collector"
    description = (
        "Usage events written by the first Claude cloud-runner collector: one summary row per "
        "sweep, without project or per-model detail."
    )
    impact = (
        "The replacement collector rebuilds this usage from each session's history and waits "
        "while these rows exist, so cloud usage is not collected, and would double count if it "
        "ran alongside them."
    )
    recommended_action = (
        "Remove the old rows. The replacement collector then collects the same sessions again "
        "with project, model and timing detail."
    )
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        plan = plan_cloud_coarse_events(session, sample_size=5)
        if not plan.events and not plan.state_rows:
            return CheckReport(check_id=self.id, severity=self.severity, total_count=0, groups=[])
        count = plan.events or plan.state_rows
        label = (
            f"{plan.events} event(s) across {plan.sessions} session(s)"
            if plan.events
            else f"{plan.state_rows} leftover state row(s)"
        )
        group = FindingGroup(
            key=GROUP_KEY,
            label=label,
            count=count,
            fixable=True,
            samples=[
                Finding(label=event_id, detail={"event_id": event_id}) for event_id in plan.samples
            ],
            detail={
                "events": plan.events,
                "sessions": plan.sessions,
                "state_rows": plan.state_rows,
            },
        )
        return CheckReport(
            check_id=self.id, severity=self.severity, total_count=count, groups=[group]
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        plan = plan_cloud_coarse_events(session, sample_size=5)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Remove {plan.events} coarse cloud usage event(s) and "
                f"{plan.state_rows} state row(s); rollups and windows are rebuilt"
            ),
            counts={
                "events": plan.events,
                "sessions": plan.sessions,
                "state_rows": plan.state_rows,
                "tokens_input": plan.tokens_input,
                "tokens_output": plan.tokens_output,
                "tokens_cache_read": plan.tokens_cache_read,
                "tokens_cache_create": plan.tokens_cache_create,
            },
            samples=[
                Finding(label=event_id, detail={"event_id": event_id}) for event_id in plan.samples
            ],
            confirmation_text="I confirm the first collector's cloud usage rows can be removed.",
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        result = apply_cloud_coarse_events(session)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=(
                    f"Removed {result.events_deleted} coarse cloud usage event(s) and "
                    f"{result.state_rows_deleted} state row(s)"
                ),
                counts={
                    "events_deleted": result.events_deleted,
                    "state_rows_deleted": result.state_rows_deleted,
                    "rollup_pairs_rebuilt": result.rollup_pairs_rebuilt,
                    "windows_rebuilt": result.windows_rebuilt,
                },
            ),
            [],
        )
