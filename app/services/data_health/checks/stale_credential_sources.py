"""Data Health `stale_credential_sources` check — machine-reported credential
rows nothing reports any more, or that never were credentials. See
`maintenance/stale_credential_sources.py` for the two shapes and the repair."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from app.models.db import CredentialSource
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
from app.services.maintenance.stale_credential_sources import (
    StaleSource,
    StaleSourcesPlan,
    apply_stale_sources,
    plan_stale_sources,
)

_KIND_LABEL = {"stale": "not reported recently", "not_a_credential": "not a credential"}


def _samples(plan: StaleSourcesPlan) -> list[Finding]:
    def sample(source: StaleSource) -> Finding:
        return Finding(
            label=f"{source.label} ({_KIND_LABEL[source.kind]})",
            detail={
                "provider_id": source.provider_id,
                "account_id": source.account_id,
                "source_id": source.source_id,
                "sidecar_id": source.sidecar_id,
                "kind": source.kind,
            },
        )

    return [sample(s) for s in plan.sources[:5]]


def _summary(plan: StaleSourcesPlan) -> str:
    kinds = ", ".join(
        f"{count} {_KIND_LABEL[kind]}" for kind, count in plan.counts.items() if count
    )
    return f"Forget {plan.total} credential source row(s) for {plan.provider_id} ({kinds})"


class StaleCredentialSourcesCheck(Check):
    id = "stale_credential_sources"
    title = "Credential sources no machine reports any more"
    description = (
        "A machine-reported credential row has not been seen for 14 days, or is a metadata-only "
        "lookup an older sidecar reported as a credential (an untried “Sidecar credential”)."
    )
    impact = (
        "Settings → Credentials lists credentials that no longer exist, and the account shows "
        "more credentials than it has."
    )
    recommended_action = (
        "Forget the rows. A credential that is still on a machine is reported again on its next "
        "collection."
    )
    severity = Severity.WARN

    def detect(self, session: Session, **_kwargs: Any) -> CheckReport:  # noqa: ANN401
        provider_ids = sorted(
            session.exec(select(col(CredentialSource.provider_id)).distinct()).all()
        )
        groups: list[FindingGroup] = []
        for provider_id in provider_ids:
            plan = plan_stale_sources(session, provider_id)
            if not plan.total:
                continue
            groups.append(
                FindingGroup(
                    key=provider_id,
                    label=f"{provider_id}: {plan.total} stale credential source(s)",
                    count=plan.total,
                    fixable=True,
                    samples=_samples(plan),
                    detail=dict(plan.counts),
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        plan = plan_stale_sources(session, group_key)
        if not plan.total:
            raise ValueError(f"No stale credential sources for {group_key!r}")
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=_summary(plan),
            counts=dict(plan.counts),
            samples=_samples(plan),
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        plan = apply_stale_sources(session, group_key)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=_summary(plan)
                if plan.total
                else f"No stale credential sources for {group_key}",
                counts=dict(plan.counts),
            ),
            [],
        )
