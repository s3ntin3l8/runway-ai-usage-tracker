"""Data Health `openrouter_model_events` check — OpenCode events whose model id only
OpenRouter can serve (`vendor/model:free`, `openrouter/...`) filed under an OpenCode
gateway provider (`opencode`, `opencode-free`, `opencode-byok`). See
`app/services/maintenance/openrouter_retag.py` for the fixer and why `lone_default_events`
cannot fix the BYOK ones.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from app.models.db import UsageEvent
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
from app.services.maintenance.openrouter_retag import (
    SOURCE_PROVIDER_IDS,
    TARGET_PROVIDER_ID,
    NoTargetAccountError,
    OpenRouterRetagPlan,
    apply_openrouter_retag,
    model_clause,
    plan_openrouter_retag,
)


def _samples(plan: OpenRouterRetagPlan) -> list[Finding]:
    return [
        Finding(
            label=f"{plan.provider_id}/{model}",
            detail={"provider_id": plan.provider_id, "model_id": model, "events": count},
        )
        for model, count in list(plan.by_model.items())[:5]
    ]


class OpenRouterModelEventsCheck(Check):
    id = "openrouter_model_events"
    title = "OpenRouter usage is filed under OpenCode"
    description = (
        "Usage events use a model id that only OpenRouter serves (a `:free` model or an "
        "`openrouter/` alias) but are stored under an OpenCode provider "
        "(`opencode`, `opencode-free` or `opencode-byok`)."
    )
    impact = (
        "OpenRouter usage is split across providers and missing from the OpenRouter account. "
        "Events under `opencode-byok` sit on the generic `default` account with no account "
        "to move them to."
    )
    recommended_action = (
        "Move the events onto your OpenRouter account. A copy of the same message that "
        "already exists under OpenRouter or a sibling provider is dropped, not doubled."
    )
    severity = Severity.ERROR
    blocked_by = ("config_default_keyed", "legacy_provider_ids")

    def detect(self, session: Session, **_kwargs: Any) -> CheckReport:  # noqa: ANN401
        present = {
            row[0]
            for row in session.execute(
                select(UsageEvent.provider_id)
                .distinct()
                .where(col(UsageEvent.provider_id).in_(SOURCE_PROVIDER_IDS), model_clause())
            )
        }
        groups: list[FindingGroup] = []
        for provider_id in sorted(present):
            plan = plan_openrouter_retag(session, provider_id)
            groups.append(
                FindingGroup(
                    key=provider_id,
                    label=(f"{provider_id}: {plan.total} event(s) served by {TARGET_PROVIDER_ID}"),
                    count=plan.total,
                    fixable=plan.target_account is not None,
                    samples=_samples(plan),
                    detail={
                        "duplicates": plan.duplicates,
                        "to_move": plan.retagged,
                        "target_account": plan.target_account,
                        **(
                            {}
                            if plan.target_account
                            else {"reason": f"needs exactly one {TARGET_PROVIDER_ID} account"}
                        ),
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
        plan = plan_openrouter_retag(session, group_key)
        if not plan.total:
            raise ValueError(f"No OpenRouter-served events under {group_key!r}")
        if plan.target_account is None:
            raise NoTargetAccountError(
                f"Need exactly one non-default {TARGET_PROVIDER_ID} account to move events onto"
            )
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Move {plan.retagged} event(s) from {group_key} onto "
                f"{TARGET_PROVIDER_ID}/{plan.target_account}"
                + (f"; drop {plan.duplicates} duplicate(s)" if plan.duplicates else "")
            ),
            counts={
                "total": plan.total,
                "duplicates": plan.duplicates,
                "to_move": plan.retagged,
            },
            samples=_samples(plan),
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        result = apply_openrouter_retag(session, group_key)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=(
                    f"Moved {result.retagged} event(s) from {group_key} onto "
                    f"{TARGET_PROVIDER_ID}/{result.target_account}"
                ),
                counts={
                    "retagged": result.retagged,
                    "duplicates_resolved": result.duplicates_resolved,
                    "rollup_pairs_rebuilt": result.rollup_pairs_rebuilt,
                    "windows_rebuilt": result.windows_rebuilt,
                },
            ),
            [],
        )
