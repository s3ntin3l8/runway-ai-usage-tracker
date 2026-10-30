"""Data Health `cloud_events_reset`: re-collect the Claude cloud-runner history under the current rules.

Cloud events are deduplicated on `(provider_id, event_id)`, so events stored before the cloud source id
(`anthropic-cloud`) and the single-repository project rule keep their old labels. This fix removes the
events and the collector's per-session checkpoints so the next cycle reads every session again. See
`app/services/maintenance/cloud_events_reset.py` and `docs/claude-cloud-event-contract.md`.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.services.claude_cloud_support import CLOUD_SIDECAR_ID
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
from app.services.maintenance.cloud_events_reset import (
    apply_cloud_events_reset,
    plan_cloud_events_reset,
)

GROUP_KEY = "stale_labels"


class CloudEventsResetCheck(Check):
    id = "cloud_events_reset"
    title = "Claude cloud usage with outdated labels"
    description = (
        "Claude cloud-runner usage events stored under another source id than the current "
        f"`{CLOUD_SIDECAR_ID}`, such as the generic `server`."
    )
    impact = (
        "The Source filter and host column show these events under the old id. Stored events are "
        "never relabelled, so only a re-collection can correct them. A session whose history is no "
        "longer readable upstream loses its stored usage for good."
    )
    recommended_action = (
        "Remove the cloud events and the collector's per-session checkpoints. The collector then "
        "reads every session's history again with the current labels."
    )
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        plan = plan_cloud_events_reset(session, sample_size=5)
        stale = [sid for sid in plan.sidecar_ids if sid != CLOUD_SIDECAR_ID]
        if not plan.events or not stale:
            return CheckReport(check_id=self.id, severity=self.severity, total_count=0, groups=[])
        group = FindingGroup(
            key=GROUP_KEY,
            label=f"{plan.events} event(s) across {plan.sessions} session(s)",
            count=plan.events,
            fixable=True,
            samples=[
                Finding(label=event_id, detail={"event_id": event_id}) for event_id in plan.samples
            ],
            detail={
                "events": plan.events,
                "sessions": plan.sessions,
                "checkpoints": plan.checkpoints,
                "windows": plan.windows,
            },
        )
        return CheckReport(
            check_id=self.id, severity=self.severity, total_count=plan.events, groups=[group]
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        plan = plan_cloud_events_reset(session, sample_size=5)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Remove {plan.events} cloud usage event(s) and {plan.checkpoints} session "
                f"checkpoint(s); the Anthropic rollups and {plan.windows} closed window(s) are "
                "rebuilt and the collector reads the history again"
            ),
            counts={
                "events": plan.events,
                "sessions": plan.sessions,
                "checkpoints": plan.checkpoints,
                "windows_to_rebuild": plan.windows,
            },
            samples=[
                Finding(label=event_id, detail={"event_id": event_id}) for event_id in plan.samples
            ],
            confirmation_text=(
                "I confirm the Claude cloud usage can be removed and collected again."
            ),
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        result = apply_cloud_events_reset(session)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=(
                    f"Removed {result.events_deleted} cloud usage event(s) and "
                    f"{result.checkpoints_deleted} checkpoint(s)"
                ),
                counts={
                    "events_deleted": result.events_deleted,
                    "checkpoints_deleted": result.checkpoints_deleted,
                    "rollup_pairs_rebuilt": result.rollup_pairs_rebuilt,
                    "windows_rebuilt": result.windows_rebuilt,
                    "removed_from": result.ts_min.isoformat() if result.ts_min else None,
                    "removed_to": result.ts_max.isoformat() if result.ts_max else None,
                },
            ),
            [],
        )
