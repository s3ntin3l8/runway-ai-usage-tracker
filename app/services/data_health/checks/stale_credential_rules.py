"""Data Health `stale_credential_rules` check — operator assignment rules that no
machine-reported credential matches any more. See
`maintenance/stale_credential_rules.py` for what is (and deliberately is not)
considered, and for the repair."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from app.models.db import CredentialTag
from app.services.credential_sources import describe_origin_full
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
from app.services.maintenance.stale_credential_rules import (
    StaleRule,
    StaleRulesPlan,
    apply_stale_rules,
    plan_stale_rules,
)


def _samples(plan: StaleRulesPlan) -> list[Finding]:
    def sample(rule: StaleRule) -> Finding:
        origin = describe_origin_full(rule.credential_origin)
        seen = (
            f"last matched {rule.last_matched_at:%Y-%m-%d}"
            if rule.last_matched_at
            else "never matched"
        )
        return Finding(
            label=f"{origin.label} → {rule.account_id} ({rule.scope}; {seen})",
            detail={
                "provider_id": rule.provider_id,
                "credential_origin": rule.credential_origin,
                "sidecar_id": rule.sidecar_id,
                "last_matched_at": rule.last_matched_at.isoformat()
                if rule.last_matched_at
                else None,
            },
        )

    return [sample(r) for r in plan.rules[:5]]


def _summary(plan: StaleRulesPlan) -> str:
    return f"Delete {plan.total} assignment rule(s) for {plan.provider_id} that no machine matches"


class StaleCredentialRulesCheck(Check):
    id = "stale_credential_rules"
    title = "Assignment rules no machine matches"
    description = (
        "An assignment rule you created points at a credential origin that no machine has "
        "reported for 14 days (or ever). Rules for `provider:` fallbacks, redirects and the "
        "ones Runway creates itself are never listed."
    )
    impact = (
        "Settings → Credentials → Rules lists rules that do nothing, and a broad all-machines "
        "path rule can attach a different credential to the old account if one appears there."
    )
    recommended_action = (
        "Delete the rule. If the credential is still unidentified it comes back under "
        "“Needs mapping”."
    )
    severity = Severity.WARN

    def detect(self, session: Session, **_kwargs: Any) -> CheckReport:  # noqa: ANN401
        provider_ids = sorted(session.exec(select(col(CredentialTag.provider_id)).distinct()).all())
        groups: list[FindingGroup] = []
        for provider_id in provider_ids:
            plan = plan_stale_rules(session, provider_id)
            if not plan.total:
                continue
            groups.append(
                FindingGroup(
                    key=provider_id,
                    label=f"{provider_id}: {plan.total} stale assignment rule(s)",
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
        plan = plan_stale_rules(session, group_key)
        if not plan.total:
            raise ValueError(f"No stale assignment rules for {group_key!r}")
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
        plan = apply_stale_rules(session, group_key)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=_summary(plan)
                if plan.total
                else f"No stale assignment rules for {group_key}",
                counts=dict(plan.counts),
            ),
            [],
        )
