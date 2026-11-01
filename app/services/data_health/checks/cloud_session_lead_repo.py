"""Data Health `cloud_session_lead_repo` check — a multi-repository Claude cloud
session whose events carry no label.

The cloud-runner collector names the repositories a session touched, but the
reducer labels an event with a repository only when the session names exactly
one or when the event's working directory matches one of the names
(`app/services/claude_cloud_events.py::_project`). A session that names several
repositories and never ran in a matching directory therefore leaves most of its
events unlabelled, so its usage is split away from the repository it belongs
to.

The repair is the session relabel in
`app/services/maintenance/cloud_session_lead_repo.py`: the session's *lead
repository* — the `project_ref` its labelled events name most often — is
applied to its unlabelled events. The evidence is the labels the session
already carries; the order of the repositories in the session list is
alphabetical and means nothing, so it is never consulted. A session whose
leading repositories tie, or that carries no label at all, is left alone and
listed with the reason.

The finding carries counts only: the session id and the repository names stay
out of the label and the detail. The repository itself appears in the sample,
where the sibling label checks show the before/after label pair.
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
from app.services.maintenance.cloud_session_lead_repo import (
    apply_cloud_session_lead_repo,
    plan_cloud_session_lead_repo,
    plan_cloud_session_lead_repos,
)

_GROUP = "cloud_session_lead_repo"

_STALE_SUMMARY = "This finding is stale — rescan to see the current groups."

_CONFIRMATION = (
    "I confirm these cloud sessions belong to the repository their labelled events name."
)

_TIE_REASON = "Two repositories tie for the most events in this session — it is left alone rather than guessed."

_NO_EVIDENCE_REASON = (
    "This session names no repository at all, so there is no evidence for one — it is left alone."
)


def _group_label(unlabelled: int) -> str:
    """The group's label: counts only, never the session id or a repository."""
    return f"{unlabelled} unlabelled event(s) in a multi-repository Claude cloud session"


def _group_detail(unlabelled: int, labelled: int, candidate_repos: int) -> dict[str, Any]:
    """The group's detail: counts only."""
    return {
        "unlabelled_events": unlabelled,
        "labelled_events": labelled,
        "candidate_repos": candidate_repos,
    }


class CloudSessionLeadRepoCheck(Check):
    id = "cloud_session_lead_repo"
    title = "A cloud session's events carry no project label"
    description = (
        "A Claude cloud session that names several repositories leaves most of its events "
        "unlabelled, because the collector only labels an event when the session names one "
        "repository or the event ran in a directory that matches one of the names. The usage "
        "of one repository is then split away from it."
    )
    impact = (
        "The affected events have no project at all, so the repository's usage is split across "
        "projects in the project filter and the Sessions view."
    )
    recommended_action = (
        "Label the session's unlabelled events with the repository its labelled events name "
        "most often. The raw working directory and the events that already carry a label are "
        "kept, so no total moves. A session whose leading repositories tie is left alone rather "
        "than guessed."
    )
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        groups: list[FindingGroup] = []
        for plan in plan_cloud_session_lead_repos(session):
            detail = _group_detail(
                plan.unlabelled_events, plan.labelled_events, plan.candidate_repos
            )
            if not plan.fixable:
                reason = _TIE_REASON if plan.skipped_reason == "tie" else _NO_EVIDENCE_REASON
                groups.append(
                    FindingGroup(
                        key=plan.session_id,
                        label=_group_label(plan.unlabelled_events),
                        count=plan.unlabelled_events,
                        fixable=False,
                        not_fixable_reason=reason,
                        detail=detail,
                    )
                )
                continue
            groups.append(
                FindingGroup(
                    key=plan.session_id,
                    label=_group_label(plan.unlabelled_events),
                    count=plan.unlabelled_events,
                    fixable=True,
                    samples=[
                        Finding(
                            label=f"(none) → {plan.lead_repo}",
                            detail={"rows": plan.unlabelled_events},
                        )
                    ],
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
        session_plan = plan_cloud_session_lead_repo(session, group_key)
        if not session_plan.fixable:
            return FixPlan(
                check_id=self.id,
                group_key=group_key,
                summary=_STALE_SUMMARY,
                counts={"events_to_label": 0},
            )
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Label {session_plan.unlabelled_events} event(s) of this cloud session with "
                f"its lead repository ({session_plan.lead_repo})"
            ),
            counts={
                "events_to_label": session_plan.unlabelled_events,
                "labelled_events": session_plan.labelled_events,
                "candidate_repos": session_plan.candidate_repos,
            },
            samples=[
                Finding(
                    label=f"(none) → {session_plan.lead_repo}",
                    detail={"rows": session_plan.unlabelled_events},
                )
            ],
            confirmation_text=_CONFIRMATION,
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        # Re-plan at apply time: rows may have been collected since the scan.
        result = apply_cloud_session_lead_repo(session, group_key)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=(
                    f"Labelled {result.events_labelled} event(s) with the session's lead "
                    f"repository ({result.lead_repo})."
                ),
                counts={"events_labelled": result.events_labelled},
            ),
            [],
        )
