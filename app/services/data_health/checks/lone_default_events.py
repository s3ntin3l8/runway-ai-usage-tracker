"""Data Health `lone_default_events` check — events sitting alone under the
stale `default` account_id for a provider that has since been configured
under a real one (D4 in the v3.0.0 prod-cleanup audit: minimax 19.5k
messages + 58 errors). `opencode-byok` has no other configured account for
its provider, so it is reported not-fixable rather than guessing a target.

Target validation is re-derived from `provider_configs` on every `plan`/
`apply` call, never trusted verbatim from the request: `target` must name an
existing, non-archived, non-`default` account for the provider.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.db import ProviderConfig, UsageEvent
from app.services.data_health._provider_accounts import candidate_targets
from app.services.data_health.base import (
    AsyncHook,
    Check,
    CheckReport,
    Finding,
    FindingGroup,
    FixPlan,
    FixResult,
    ParamSpec,
    Severity,
)
from app.services.maintenance.event_reassign import apply_reassign_default, plan_reassign_default


class LoneDefaultEventsCheck(Check):
    id = "lone_default_events"
    title = "Usage is assigned to a generic account"
    description = "Events remain under the “default” account even though a specific account is configured for this provider."
    impact = "Usage may be missing from the account that actually generated it."
    recommended_action = "Reassign events to the suggested configured account. If there is no unambiguous target, review the account setup first."
    severity = Severity.ERROR
    blocked_by = ("config_default_keyed",)

    def detect(self, session: Session) -> CheckReport:
        active_defaults = set(
            session.exec(
                select(ProviderConfig.provider_id).where(
                    col(ProviderConfig.account_id) == "default",
                    col(ProviderConfig.archived).is_(False),
                )
            ).all()
        )
        counts = session.execute(
            select(UsageEvent.provider_id, UsageEvent.kind, func.count())
            .where(col(UsageEvent.account_id) == "default")
            .group_by(UsageEvent.provider_id, UsageEvent.kind)
        ).all()
        by_provider: dict[str, dict[str, int]] = {}
        for provider_id, kind, n in counts:
            if provider_id in active_defaults:
                continue
            by_provider.setdefault(provider_id, {})[kind] = n

        groups: list[FindingGroup] = []
        for provider_id, kinds in sorted(by_provider.items()):
            total = sum(kinds.values())
            candidates = candidate_targets(session, provider_id)
            sample = Finding(
                label=provider_id, detail={"provider_id": provider_id, "by_kind": kinds}
            )
            if len(candidates) == 1:
                groups.append(
                    FindingGroup(
                        key=provider_id,
                        label=f"{provider_id}: default → {candidates[0]}",
                        count=total,
                        fixable=True,
                        params=[
                            ParamSpec(name="target", label="Target account", options=candidates)
                        ],
                        samples=[sample],
                        detail={"by_kind": kinds, "suggested_target": candidates[0]},
                    )
                )
            else:
                reason = (
                    "no configured non-default account for this provider"
                    if not candidates
                    else f"multiple candidate accounts, pick one: {', '.join(candidates)}"
                )
                groups.append(
                    FindingGroup(
                        key=provider_id,
                        label=f"{provider_id}: {total} event(s) under default",
                        count=total,
                        fixable=bool(candidates),
                        not_fixable_reason=None if candidates else reason,
                        params=(
                            [ParamSpec(name="target", label="Target account", options=candidates)]
                            if candidates
                            else []
                        ),
                        samples=[sample],
                        detail={"by_kind": kinds, "candidates": candidates},
                    )
                )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def _resolve_target(self, session: Session, provider_id: str, params: dict[str, Any]) -> str:
        active_default = session.exec(
            select(ProviderConfig).where(
                col(ProviderConfig.provider_id) == provider_id,
                col(ProviderConfig.account_id) == "default",
                col(ProviderConfig.archived).is_(False),
            )
        ).first()
        if active_default is not None:
            raise ValueError(f"{provider_id!r} still has an active default config; rekey it first")
        candidates = candidate_targets(session, provider_id)
        target = params.get("target")
        if not target:
            if len(candidates) == 1:
                target = candidates[0]
            else:
                raise ValueError(
                    f"{provider_id!r} has no unambiguous target account; "
                    f"specify one of {candidates}"
                )
        target = str(target)
        if target == "default":
            raise ValueError("target account cannot be 'default'")
        if target not in candidates:
            raise ValueError(
                f"{target!r} is not a configured account for {provider_id!r}; "
                f"choose one of {candidates}"
            )
        return target

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        provider_id = group_key
        target = self._resolve_target(session, provider_id, params)
        reassign_plan = plan_reassign_default(
            session, provider_id=provider_id, source="default", target=target
        )
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=f"Reassign {provider_id}/default onto {target}",
            counts={"count": reassign_plan.count},
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        provider_id = group_key
        target = self._resolve_target(session, provider_id, params)
        result = apply_reassign_default(
            session, provider_id=provider_id, source="default", target=target
        )
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Reassigned {provider_id}/default onto {target}",
                counts={
                    "moved": result.moved,
                    "rollups_rebuilt_pairs": result.rollups_rebuilt_pairs,
                    "windows_rebuilt": result.windows_rebuilt,
                },
            ),
            [],
        )
