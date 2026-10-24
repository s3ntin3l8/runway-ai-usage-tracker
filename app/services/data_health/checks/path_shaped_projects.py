"""Data Health `path_shaped_projects` check — a project label that is a path.

The `project` label on a usage event is derived server-side from the client's
working directory. A Windows client reports `D:\\Projects\\repo`, and a
derivation that split on the *server's* separator had nothing to split on — so
the whole path became the label.

A path is not a name: the same repository lands in a different bucket on every
host and every checkout, the Sessions view shows a directory layout where a
project name belongs, and the Top Projects ranking splits one project across
its clones.

The repair is in `app/services/maintenance/project_labels.py`, which the check
and `scripts/consolidate_session_projects.py` share.
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
    apply_path_shaped_projects,
    label_map_detail,
    plan_path_shaped_projects,
)

#: The group of rows the fix can relabel.
_FIXABLE_GROUP = "path_shaped_projects"

#: The group of rows it cannot: they carry a path-shaped label but the session
#: holds no working directory a label can be derived from.
_UNFIXABLE_GROUP = "path_shaped_projects_unfixable"


def _stale_plan(check_id: str, group_key: str) -> FixPlan:
    """The plan for a group key this check does not know."""
    return FixPlan(
        check_id=check_id,
        group_key=group_key,
        summary="This finding is stale — rescan to see the current groups.",
        counts={"rows_to_relabel": 0},
    )


class PathShapedProjectsCheck(Check):
    id = "path_shaped_projects"
    title = "A project label is a path"
    description = (
        "Some usage events carry a directory path as their project label, because the "
        "label was derived on a host whose path separator the server did not recognise."
    )
    impact = (
        "One project is split across as many buckets as it has checkouts, the Sessions view "
        "shows a directory layout where a project name belongs, and the Top Projects ranking "
        "counts the same repository several times."
    )
    recommended_action = (
        "Relabel the affected sessions with the name of their project directory. The raw "
        "working directory is kept; only the label changes, so no total moves."
    )
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        # The detection reuses the repair's own query, so the check and the fix
        # can never disagree about what counts as a path-shaped label.
        plan = plan_path_shaped_projects(session)
        groups: list[FindingGroup] = []
        if plan.path_shaped_rows:
            groups.append(
                FindingGroup(
                    key=_FIXABLE_GROUP,
                    label=f"{plan.path_shaped_rows} row(s) labelled with a path",
                    count=plan.path_shaped_rows,
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
        if plan.unfixable_rows:
            groups.append(
                FindingGroup(
                    key=_UNFIXABLE_GROUP,
                    label=f"{plan.unfixable_rows} row(s) with no usable working directory",
                    count=plan.unfixable_rows,
                    fixable=False,
                    not_fixable_reason=(
                        "These sessions carry a path-shaped label but no working directory a "
                        "project name can be derived from, so there is nothing to relabel them "
                        "to. Collect more usage for them, or check the client's working "
                        "directory."
                    ),
                    detail={
                        "sessions": plan.unfixable_sessions,
                        "path_shaped_rows": plan.unfixable_rows,
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
        if group_key == _UNFIXABLE_GROUP:
            return FixPlan(
                check_id=self.id,
                group_key=group_key,
                summary="These rows have no working directory to derive a label from.",
                counts={"rows_to_relabel": 0},
            )
        if group_key != _FIXABLE_GROUP:
            # A key from an older scan (or a renamed group) must not fall
            # through to the relabel: it would rewrite every path-shaped row
            # instead of refusing.
            return _stale_plan(self.id, group_key)
        relabel_plan = plan_path_shaped_projects(session)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Relabel {relabel_plan.path_shaped_rows} row(s) to their project name "
                f"({relabel_plan.sessions} session(s))"
            ),
            counts={
                "path_shaped_rows": relabel_plan.path_shaped_rows,
                "rows_to_relabel": relabel_plan.rows_to_relabel,
                "sessions": relabel_plan.sessions,
            },
            samples=[Finding(label=sample, detail={}) for sample in relabel_plan.samples[:10]],
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        if group_key == _UNFIXABLE_GROUP:
            return (
                FixResult(
                    check_id=self.id,
                    group_key=group_key,
                    summary="Nothing to relabel — these rows have no usable working directory.",
                    counts={"rows_relabeled": 0},
                ),
                [],
            )
        if group_key != _FIXABLE_GROUP:
            # See `plan`: an unknown key is refused, never guessed at.
            return (
                FixResult(
                    check_id=self.id,
                    group_key=group_key,
                    summary="This finding is stale — rescan to see the current groups.",
                    counts={"rows_relabeled": 0},
                ),
                [],
            )
        # Re-plan at apply time: rows may have been collected since the scan.
        relabel_plan = plan_path_shaped_projects(session)
        result = apply_path_shaped_projects(session, relabel_plan)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Relabelled {result.relabeled} row(s) to their project name.",
                counts={
                    "rows_relabeled": result.relabeled,
                    "sessions": result.sessions,
                    "path_shaped_rows": result.path_shaped_rows,
                },
            ),
            [],
        )
