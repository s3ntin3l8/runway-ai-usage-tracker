"""Data Health `duplicate_telemetry_events`: Claude Code calls stored twice.

A Claude Code telemetry event (``otel|<request_id>``) whose request is also stored as a
transcript event in the same session counts that call twice. Claude Desktop's local Code
sessions produced these until the sidecar learned that their ``query_source = "sdk"`` main loop
is covered by the transcripts. See `app/services/maintenance/duplicate_telemetry_events.py`.
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
from app.services.maintenance.duplicate_telemetry_events import (
    apply_duplicate_telemetry_events,
    plan_duplicate_telemetry_events,
)

GROUP_KEY = "duplicates"


class DuplicateTelemetryEventsCheck(Check):
    id = "duplicate_telemetry_events"
    title = "Claude Code calls counted twice"
    description = (
        "Claude Code telemetry events for a request that the session's transcript already "
        "recorded, so the call is stored twice."
    )
    impact = "Tokens and cost of these calls are counted twice in usage, history and windows."
    recommended_action = (
        "Remove the telemetry copies. The transcript events stay, with their project and cost."
    )
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        plan = plan_duplicate_telemetry_events(session, sample_size=5)
        if not plan.events:
            return CheckReport(check_id=self.id, severity=self.severity, total_count=0, groups=[])
        group = FindingGroup(
            key=GROUP_KEY,
            label=f"{plan.events} call(s) across {plan.sessions} session(s)",
            count=plan.events,
            fixable=True,
            samples=[
                Finding(label=event_id, detail={"event_id": event_id}) for event_id in plan.samples
            ],
            detail={
                "events": plan.events,
                "sessions": plan.sessions,
                "cost_usd": round(plan.cost_usd, 4),
            },
        )
        return CheckReport(
            check_id=self.id, severity=self.severity, total_count=plan.events, groups=[group]
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        plan = plan_duplicate_telemetry_events(session, sample_size=5)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Remove {plan.events} duplicate telemetry event(s) "
                f"(${plan.cost_usd:.4f} counted twice); the affected Anthropic rollups and "
                "closed windows are rebuilt"
            ),
            counts={
                "events": plan.events,
                "sessions": plan.sessions,
                "tokens_output": plan.tokens_output,
                "cost_usd": round(plan.cost_usd, 4),
                "accounts": len(plan.accounts),
            },
            samples=[
                Finding(label=event_id, detail={"event_id": event_id}) for event_id in plan.samples
            ],
            confirmation_text="I confirm the duplicate telemetry events can be removed.",
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        result = apply_duplicate_telemetry_events(session)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Removed {result.events_deleted} duplicate telemetry event(s)",
                counts={
                    "events_deleted": result.events_deleted,
                    "rollup_pairs_rebuilt": result.rollup_pairs_rebuilt,
                    "windows_rebuilt": result.windows_rebuilt,
                },
            ),
            [],
        )
