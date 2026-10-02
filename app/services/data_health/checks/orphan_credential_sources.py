"""Data Health `orphan_credential_sources` check — a `credential_sources`
row still keyed to an account with nothing behind it: no configuration, quota
card, usage or operator-set tag/label. The row is a dead claim on an identity
nothing collects for.

Two shapes (see `credential_sources_repair.py` for the repair): a `config:`
row whose `provider_configs` row is gone, and a source id filed under two
accounts where only one of them still has evidence. Both are leftovers of an
account rename that did not carry its credential rows along — the shape
`config_default_keyed`'s rekey used to produce, and still the reason it blocks
this check: fix the rekey first (it now moves the rows with it) and only then
delete what an older build stranded.
"""

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
from app.services.maintenance.credential_sources_repair import (
    OrphanSource,
    OrphanSourcesPlan,
    apply_orphan_sources,
    plan_orphan_sources,
)

_KIND_LABEL = {"config_ghost": "stale configuration", "duplicate": "duplicate source"}


def _sample_detail(orphan: OrphanSource) -> dict[str, Any]:
    return {
        "provider_id": orphan.provider_id,
        "account_id": orphan.account_id,
        "source_id": orphan.source_id,
        "sidecar_id": orphan.sidecar_id,
        "kind": orphan.kind,
    }


def _summary(plan: OrphanSourcesPlan) -> str:
    kinds = ", ".join(
        f"{count} {_KIND_LABEL[kind]}"
        for kind, count in (("config_ghost", plan.config_ghosts), ("duplicate", plan.duplicates))
        if count
    )
    return f"Delete {plan.total} stranded credential source row(s) for {plan.provider_id} ({kinds})"


class OrphanCredentialSourcesCheck(Check):
    id = "orphan_credential_sources"
    title = "Credential sources point to a missing account"
    description = (
        "A stored credential source no longer has anything behind it: its configuration was "
        "re-keyed or removed (a `config:` row is only ever written beside one), or the same "
        "source is filed under two accounts and only one of them still has evidence."
    )
    impact = (
        "The provider can be listed twice in Settings → Credentials, and the stranded row keeps "
        "a claim — enabled, priority, health — for an account nothing collects against."
    )
    recommended_action = "Delete the stranded row; the copy under the real account stays."
    severity = Severity.WARN
    blocked_by = ("config_default_keyed",)

    def detect(self, session: Session) -> CheckReport:
        provider_ids = sorted(
            session.exec(select(col(CredentialSource.provider_id)).distinct()).all()
        )
        groups: list[FindingGroup] = []
        for provider_id in provider_ids:
            plan = plan_orphan_sources(session, provider_id)
            if not plan.total:
                continue
            groups.append(
                FindingGroup(
                    key=provider_id,
                    label=f"{provider_id}: {plan.total} stranded credential source(s)",
                    count=plan.total,
                    fixable=True,
                    samples=[
                        Finding(label=orphan.label, detail=_sample_detail(orphan))
                        for orphan in plan.orphans[:5]
                    ],
                    detail={
                        "config_ghosts": plan.config_ghosts,
                        "duplicates": plan.duplicates,
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
        plan = plan_orphan_sources(session, group_key)
        if not plan.total:
            raise ValueError(f"No stranded credential sources for {group_key!r}")
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=_summary(plan),
            counts=plan.counts,
            samples=[
                Finding(label=orphan.label, detail=_sample_detail(orphan))
                for orphan in plan.orphans[:5]
            ],
            confirmation_text=(
                f"I confirm these {plan.total} credential source row(s) are stranded: their "
                f"configuration is gone, or the account holding them has no configuration, "
                f"card, usage or tag/label — and that the copy under the real account is kept."
            ),
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        if params.get("same_account_confirmed") is not True:
            raise ValueError("Confirm the stranded rows are stale before deleting them")
        plan = apply_orphan_sources(session, provider_id=group_key)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=_summary(plan)
                if plan.total
                else f"No stranded credential sources for {group_key}",
                counts=plan.counts,
            ),
            [],
        )
