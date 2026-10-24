"""Data Health `inconsistent_project_labels` check — a label that does not follow
the repository the client reported.

The client resolves the git repository of a working directory on its own host
(the server never reads the client's filesystem) and reports the repository's
*name*. Every event carries that name in `project_ref`, and the label on the row
follows it.

The rows collected before the client reported one keep the label they were
given — the name of the folder the work happened in. When the same working
directory later reports a repository, the older rows are left behind in a
bucket of their own: the same repository, split across two projects.

This check finds those rows and offers to move them onto the repository name.
A working directory that never reports one again keeps its label — the server
cannot know which repository it belonged to.
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
    apply_repo_relabels,
    plan_repo_relabels,
)

#: The group of rows whose label does not follow the reported repository.
_GROUP = "inconsistent_project_labels"

#: The wording for a group key this check does not know. One string, so the
#: plan and the result the UI shows side by side cannot drift apart.
_STALE_SUMMARY = "This finding is stale — rescan to see the current groups."


def _stale_plan(check_id: str, group_key: str) -> FixPlan:
    """The plan for a group key this check does not know."""
    return FixPlan(
        check_id=check_id,
        group_key=group_key,
        summary=_STALE_SUMMARY,
        counts={"rows_to_relabel": 0},
    )


def _stale_result(check_id: str, group_key: str) -> FixResult:
    """The result for a group key this check does not know."""
    return FixResult(
        check_id=check_id,
        group_key=group_key,
        summary=_STALE_SUMMARY,
        counts={"rows_relabeled": 0},
    )


class InconsistentProjectLabelsCheck(Check):
    id = "inconsistent_project_labels"
    title = "A project label does not follow the reported repository"
    description = (
        "Some usage events are labelled with the name of the folder the work happened in, "
        "while newer events from the same directory carry the name of the git repository it "
        "belongs to — so one repository is split across two projects."
    )
    impact = (
        "The same repository is counted twice in the Top Projects ranking and shows up under "
        "two names in the Sessions view."
    )
    recommended_action = (
        "Move the affected rows onto the repository name the client reported. The working "
        "directory and the reported repository are kept, so no total moves."
    )
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        # The detection reuses the repair's own query, so the check and the fix
        # can never disagree about what counts as a stale label.
        plan = plan_repo_relabels(session)
        groups: list[FindingGroup] = []
        if plan.total:
            groups.append(
                FindingGroup(
                    key=_GROUP,
                    label=f"{plan.total} row(s) labelled with a folder name",
                    count=plan.total,
                    fixable=True,
                    samples=[Finding(label=sample, detail={}) for sample in plan.samples[:10]],
                    detail={
                        "directories": len(plan.relabels),
                        "rows": plan.total,
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
        if group_key != _GROUP:
            # A key from an older scan (or a renamed group) must not fall
            # through to the relabel: it would rewrite every row it can reach
            # instead of refusing.
            return _stale_plan(self.id, group_key)
        relabel_plan = plan_repo_relabels(session)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Move {relabel_plan.total} row(s) onto the repository name the client reported"
            ),
            counts={
                "rows_to_relabel": relabel_plan.total,
                "directories": len(relabel_plan.relabels),
            },
            samples=[Finding(label=sample, detail={}) for sample in relabel_plan.samples[:10]],
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        if group_key != _GROUP:
            # See `plan`: an unknown key is refused, never guessed at.
            return (_stale_result(self.id, group_key), [])
        # Re-plan at apply time: rows may have been collected since the scan.
        relabel_plan = plan_repo_relabels(session)
        result = apply_repo_relabels(session, relabel_plan)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Moved {result.relabeled} row(s) onto the reported repository name.",
                counts={
                    "rows_relabeled": result.relabeled,
                    "directories": result.directories,
                },
            ),
            [],
        )
