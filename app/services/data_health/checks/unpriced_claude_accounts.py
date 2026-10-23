"""Data Health `unpriced_claude_accounts` check — Claude accounts whose billing
model nobody ever decided.

A detected billing model only ever lands on a **new** account row, so every
Claude account created before the detection existed keeps
``billing_type = "unknown"`` and the dashboard shows a usage value where a
plan shows an estimate. See `app/services/maintenance/claude_billing_type.py`
for the repair, which reads the account's own published cards.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.services.data_health.base import (
    Check,
    CheckReport,
    Finding,
    FindingGroup,
    FixPlan,
    FixResult,
    Severity,
)
from app.services.maintenance.claude_billing_type import (
    CLAUDE_PROVIDERS,
    apply_claude_billing_type,
    plan_claude_billing_type,
)

#: Suffix of the group that reports the accounts no published card can price.
#: Those are reported, never guessed at, and the fixer does not touch them —
#: so they sit in a group of their own rather than inflating the count of
#: the group the fixer can act on.
UNPRICEABLE_SUFFIX = "::unpriceable"


class UnpricedClaudeAccountsCheck(Check):
    id = "unpriced_claude_accounts"
    title = "Claude accounts without a billing model"
    description = (
        "Some Claude accounts never had their billing model detected, so the "
        "dashboard shows a usage value where a plan shows an estimate."
    )
    impact = (
        "Money is presented under the wrong label: a plan reads as a plain usage "
        "value, and a pay-as-you-go account reads as an estimate."
    )
    recommended_action = (
        "Price the accounts the preview lists. An account you set by hand is never touched."
    )
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        groups: list[FindingGroup] = []
        for provider_id in CLAUDE_PROVIDERS:
            plan = plan_claude_billing_type(session, provider_id=provider_id)
            if not plan.total:
                continue
            if plan.priced:
                groups.append(
                    FindingGroup(
                        key=provider_id,
                        label=f"{provider_id}: {len(plan.priced)} account(s) to price",
                        count=len(plan.priced),
                        fixable=True,
                        samples=[
                            Finding(
                                label=candidate.account_id,
                                detail={
                                    "account_id": candidate.account_id,
                                    "billing_type": candidate.model,
                                },
                            )
                            for candidate in plan.priced[:5]
                        ],
                        detail={
                            "priced": len(plan.priced),
                            "skipped": len(plan.skipped),
                        },
                    )
                )
            if plan.skipped:
                groups.append(
                    FindingGroup(
                        key=f"{provider_id}{UNPRICEABLE_SUFFIX}",
                        label=(
                            f"{provider_id}: {len(plan.skipped)} account(s) no published card prices"
                        ),
                        count=len(plan.skipped),
                        fixable=False,
                        not_fixable_reason=(
                            "no published card decides a billing model for these accounts; "
                            "set the billing model by hand"
                        ),
                        samples=[
                            Finding(
                                label=candidate.account_id,
                                detail={
                                    "account_id": candidate.account_id,
                                    "billing_type": candidate.model,
                                },
                            )
                            for candidate in plan.skipped[:5]
                        ],
                        detail={
                            "priced": len(plan.priced),
                            "skipped": len(plan.skipped),
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
        provider_id = self._provider_of(group_key)
        plan = plan_claude_billing_type(session, provider_id=provider_id)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=f"Price {len(plan.priced)} {provider_id} account(s)",
            counts={
                "priced": len(plan.priced),
                "skipped": len(plan.skipped),
                "total": plan.total,
            },
            samples=[
                Finding(
                    label=candidate.account_id,
                    detail={
                        "account_id": candidate.account_id,
                        "billing_type": candidate.model,
                    },
                )
                for candidate in plan.priced[:5]
            ],
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[Any]]:
        provider_id = self._provider_of(group_key)
        result = apply_claude_billing_type(session, provider_id=provider_id)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Priced {result.priced} {provider_id} account(s)",
                counts={"priced": result.priced, "skipped": result.skipped},
            ),
            [],
        )

    @staticmethod
    def _provider_of(group_key: str) -> str:
        """The provider a group key names.

        A key that names no Claude provider is a mistake, not an empty group,
        so it is refused rather than answered with an empty plan.
        """
        if group_key.endswith(UNPRICEABLE_SUFFIX):
            raise ValueError("accounts no published card prices cannot be priced automatically")
        provider_id = group_key.removesuffix(UNPRICEABLE_SUFFIX)
        if provider_id not in CLAUDE_PROVIDERS:
            raise ValueError(f"unknown Claude provider {provider_id!r}")
        return provider_id
