"""Data Health `orphan_credential_tags` check — a credential_tags row still
pointing at `account_id="default"` after the provider's `default` config
was rekeyed or removed (see `config_default_keyed`), so the sidecar's next
`/fleet/config` read hints an account that no longer resolves to anything.

Two fix actions: `delete` (drop the stale tag; the sidecar reports the
origin again and the operator retags it) or `repoint` (move it onto one of
the provider's other configured accounts). `credential_tags` has no
uniqueness on `account_id`, so this is a plain per-row loop — the table is
small (one row per credential origin, never per event).
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from app.models.db import CredentialTag, ProviderConfig
from app.services.credential_tags import CredentialTagRepo
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


def _has_default_config(session: Session, provider_id: str) -> bool:
    return (
        session.exec(
            select(ProviderConfig).where(
                col(ProviderConfig.provider_id) == provider_id,
                col(ProviderConfig.account_id) == "default",
                col(ProviderConfig.archived).is_(False),
            )
        ).first()
        is not None
    )


def _orphaned_tags(session: Session, provider_id: str) -> list[CredentialTag]:
    if _has_default_config(session, provider_id):
        return []
    return [
        t
        for t in CredentialTagRepo.list_for_account_provider(session, provider_id=provider_id)
        if t.account_id == "default"
    ]


class OrphanCredentialTagsCheck(Check):
    id = "orphan_credential_tags"
    title = "Credential tags point to a missing account"
    description = "A credential tag still refers to the provider’s “default” account after that configuration was re-keyed or removed."
    impact = "Sidecars may keep attributing credentials to an account that no longer exists."
    recommended_action = "Delete the stale tag or repoint it to a configured account."
    severity = Severity.WARN
    blocked_by = ("config_default_keyed",)

    def detect(self, session: Session) -> CheckReport:
        tags = session.exec(
            select(CredentialTag).where(col(CredentialTag.account_id) == "default")
        ).all()
        provider_ids = sorted({t.target_provider_id or t.provider_id for t in tags})

        groups: list[FindingGroup] = []
        for provider_id in provider_ids:
            orphans = _orphaned_tags(session, provider_id)
            if not orphans:
                continue  # this provider's `default` tags point at a real config
            candidates = candidate_targets(session, provider_id)
            groups.append(
                FindingGroup(
                    key=provider_id,
                    label=f"{provider_id}: {len(orphans)} tag(s) pointing at default",
                    count=len(orphans),
                    fixable=True,  # delete is always available, even with no repoint target
                    params=[
                        ParamSpec(name="action", label="Action", options=["delete", "repoint"]),
                        ParamSpec(
                            name="target",
                            label="Target account (repoint only)",
                            required=False,
                            options=candidates or None,
                        ),
                    ],
                    samples=[
                        Finding(
                            label=t.credential_origin,
                            detail={
                                "provider_id": t.provider_id,
                                "credential_origin": t.credential_origin,
                                "sidecar_id": t.sidecar_id,
                            },
                        )
                        for t in orphans[:5]
                    ],
                    detail={"candidates": candidates},
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def _resolve_repoint_target(
        self, session: Session, provider_id: str, params: dict[str, Any]
    ) -> str:
        candidates = candidate_targets(session, provider_id)
        target = params.get("target")
        if not target or target not in candidates:
            raise ValueError(f"repoint requires a target in {candidates}")
        return str(target)

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        provider_id = group_key
        tags = _orphaned_tags(session, provider_id)
        action = params.get("action", "delete")
        if action == "repoint":
            target = self._resolve_repoint_target(session, provider_id, params)
            summary = f"Repoint {len(tags)} tag(s) for {provider_id} onto {target}"
        elif action == "delete":
            summary = f"Delete {len(tags)} orphan tag(s) for {provider_id}"
        else:
            raise ValueError(f"unknown action {action!r}; expected 'delete' or 'repoint'")
        return FixPlan(
            check_id=self.id, group_key=group_key, summary=summary, counts={"tags": len(tags)}
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        provider_id = group_key
        action = params.get("action", "delete")
        if action == "repoint":
            target = self._resolve_repoint_target(session, provider_id, params)
            tags = _orphaned_tags(session, provider_id)
            for tag in tags:
                tag.account_id = target
                session.add(tag)
            session.commit()
            summary = f"Repointed {len(tags)} tag(s) for {provider_id} onto {target}"
            counts = {"tags_repointed": len(tags)}
        elif action == "delete":
            # Delete exactly the tags `_orphaned_tags` currently finds — not
            # a blanket delete_by_account, which would also drop a tag for
            # a provider that (by now, or always) has a real default
            # config and so was never orphaned in the first place.
            tags = _orphaned_tags(session, provider_id)
            for tag in tags:
                session.delete(tag)
            session.commit()
            summary = f"Deleted {len(tags)} orphan tag(s) for {provider_id}"
            counts = {"tags_deleted": len(tags)}
        else:
            raise ValueError(f"unknown action {action!r}; expected 'delete' or 'repoint'")
        return (
            FixResult(check_id=self.id, group_key=group_key, summary=summary, counts=counts),
            [],
        )
