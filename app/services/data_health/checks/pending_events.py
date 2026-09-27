"""Data Health `pending_events` check — informational pointer to the
Fleet page's pending-event quarantine (#359): events awaiting evidence-backed
or manual account assignment. Not fixed here — assignment is a per-event
operator decision (`POST /fleet/events/pending/assign`), not something this
check should guess at, so it only surfaces the count and a link.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlmodel import Session, select

from app.models.db import PendingUsageEvent
from app.services.data_health.base import Check, CheckReport, Finding, FindingGroup, Severity


class PendingEventsCheck(Check):
    id = "pending_events"
    severity = Severity.INFO

    def detect(self, session: Session) -> CheckReport:
        total = session.exec(select(func.count()).select_from(PendingUsageEvent)).one()
        if not total:
            return CheckReport(check_id=self.id, severity=self.severity, total_count=0, groups=[])
        groups = [
            FindingGroup(
                key="pending",
                label=f"{total} event(s) awaiting account assignment",
                count=total,
                fixable=False,
                not_fixable_reason="assign each event to an account on the Fleet page",
                samples=[
                    Finding(
                        label="pending events",
                        detail={"count": total, "link": "/fleet#pending-events"},
                    )
                ],
                detail={"link": "/fleet#pending-events"},
            )
        ]
        return CheckReport(
            check_id=self.id, severity=self.severity, total_count=total, groups=groups
        )
