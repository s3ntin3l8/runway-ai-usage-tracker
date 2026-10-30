"""Data Health `worktree_project_labels` check — a label named after a worktree.

A session run in `<repo>/.wt/<slug>` (or `.worktrees`, `.worktree`,
`.mullion-worktrees`) used to be labelled with the worktree's own directory, so
every worktree became its own project and the project filter filled up with
slugs. `normalize_cwd` now cuts the cwd at those directories (see
`app/services/project_label.py`), which fixes new events; the rows already
stored keep the label they were given until this check relabels them.

The repair is the session relabel in `app/services/maintenance/project_labels.py`
(shared with `scripts/consolidate_session_projects.py`). A session that reported
a repository name keeps it: the relabel and `inconsistent_project_labels` agree,
so applying one leaves nothing for the other.
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
from app.services.maintenance.project_labels import (
    apply_session_relabels,
    label_map_detail,
    plan_worktree_relabels,
)

_GROUP = "worktree_project_labels"

_STALE_SUMMARY = "This finding is stale — rescan to see the current groups."

_CONFIRMATION = "I confirm these projects were labelled after worktree directories."


class WorktreeProjectLabelsCheck(Check):
    id = "worktree_project_labels"
    title = "A project label is named after a worktree"
    description = (
        "Some usage events are labelled with the name of a worktree directory "
        "(`.wt/<slug>`, `.worktrees/<slug>`, ...) instead of the repository it belongs to, "
        "so every worktree shows up as a project of its own."
    )
    impact = (
        "The project filter fills with worktree names, and one repository is split across "
        "as many projects as it had worktrees."
    )
    recommended_action = (
        "Relabel the affected sessions with the name of their repository directory. The raw "
        "working directory and any reported repository are kept; only the label changes, "
        "so no total moves. Back up the database first."
    )
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        plan = plan_worktree_relabels(session)
        groups: list[FindingGroup] = []
        if plan.rows_to_relabel:
            groups.append(
                FindingGroup(
                    key=_GROUP,
                    label=f"{plan.rows_to_relabel} row(s) labelled after a worktree directory",
                    count=plan.rows_to_relabel,
                    fixable=True,
                    samples=[
                        Finding(
                            label=f"{old} → {new}",
                            detail={"from": old, "to": new, "rows": count},
                        )
                        for old, new, count in plan.label_map[:10]
                    ],
                    detail=label_map_detail(plan),
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        if group_key != _GROUP:
            return FixPlan(
                check_id=self.id,
                group_key=group_key,
                summary=_STALE_SUMMARY,
                counts={"rows_to_relabel": 0},
            )
        relabel_plan = plan_worktree_relabels(session)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Relabel {relabel_plan.rows_to_relabel} row(s) to their repository "
                f"directory ({relabel_plan.sessions} session(s))"
            ),
            counts={
                "rows_to_relabel": relabel_plan.rows_to_relabel,
                "sessions": relabel_plan.sessions,
                "labels": len(relabel_plan.label_map),
            },
            samples=[Finding(label=sample, detail={}) for sample in relabel_plan.samples[:10]],
            confirmation_text=_CONFIRMATION,
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        if group_key != _GROUP:
            return (
                FixResult(
                    check_id=self.id,
                    group_key=group_key,
                    summary=_STALE_SUMMARY,
                    counts={"rows_relabeled": 0},
                ),
                [],
            )
        # Re-plan at apply time: rows may have been collected since the scan.
        result = apply_session_relabels(session, plan_worktree_relabels(session))
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Relabelled {result.relabeled} row(s) to their repository directory.",
                counts={"rows_relabeled": result.relabeled, "sessions": result.sessions},
            ),
            [],
        )
