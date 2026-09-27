"""Data Health `config_default_keyed` check — a provider_configs row still
keyed `account_id="default"` whose `account_label` already carries a real
identity (D11 in the v3.0.0 prod-cleanup audit: kimi, minimax, ollama,
opencode). See `app/services/maintenance/config_rekey.py` for why this
blocks `POST /fleet/events/pending/assign`.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from app.models.db import ProviderConfig
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
from app.services.maintenance.config_rekey import (
    RekeyCollisionError,
    apply_rekey_config,
    plan_rekey_config,
)

_NOT_A_REAL_LABEL = {"default", ""}


def _suggested_target(row: ProviderConfig) -> str | None:
    label = (row.account_label or "").strip()
    if label in _NOT_A_REAL_LABEL or label == row.account_id:
        return None
    return label


def _find_row(session: Session, provider_id: str) -> ProviderConfig | None:
    return session.exec(
        select(ProviderConfig).where(
            col(ProviderConfig.provider_id) == provider_id,
            col(ProviderConfig.account_id) == "default",
        )
    ).first()


class ConfigDefaultKeyedCheck(Check):
    id = "config_default_keyed"
    severity = Severity.ERROR

    def detect(self, session: Session) -> CheckReport:
        rows = session.exec(
            select(ProviderConfig).where(
                col(ProviderConfig.account_id) == "default",
                col(ProviderConfig.archived).is_(False),
            )
        ).all()
        groups: list[FindingGroup] = []
        for row in rows:
            target = _suggested_target(row)
            if target is None:
                continue
            groups.append(
                FindingGroup(
                    key=row.provider_id,
                    label=f"{row.provider_id}: default → {target}",
                    count=1,
                    fixable=True,
                    params=[
                        ParamSpec(name="new_account_id", label="New account id", required=False)
                    ],
                    samples=[
                        Finding(
                            label=row.provider_id,
                            detail={
                                "provider_id": row.provider_id,
                                "current_account_id": row.account_id,
                                "account_label": row.account_label,
                            },
                        )
                    ],
                    detail={"suggested_new_account_id": target},
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=len(groups),
            groups=groups,
        )

    def _resolve_target(self, session: Session, provider_id: str, params: dict[str, Any]) -> str:
        new_account_id = params.get("new_account_id")
        if new_account_id:
            return str(new_account_id)
        row = _find_row(session, provider_id)
        if row is None:
            raise ValueError(f"No default-keyed provider_config found for {provider_id!r}")
        target = _suggested_target(row)
        if target is None:
            raise ValueError(f"{provider_id!r} has no resolvable account_label to rekey onto")
        return target

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        provider_id = group_key
        target = self._resolve_target(session, provider_id, params)
        rekey_plan = plan_rekey_config(
            session, provider_id=provider_id, old_account_id="default", new_account_id=target
        )
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=f"Rekey {provider_id}/default onto {target}",
            counts={
                "credential_tags": rekey_plan.credential_tags,
                "webhook_configs": rekey_plan.webhook_configs,
                "webhook_configs_dropped_duplicate": rekey_plan.webhook_configs_dropped_duplicate,
                "gauge_series_merged": rekey_plan.gauge_series.merged,
                "gauge_series_retagged": rekey_plan.gauge_series.retagged,
            },
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        provider_id = group_key
        target = self._resolve_target(session, provider_id, params)
        on_collision = params.get("on_collision", "abort")
        try:
            result, hooks = apply_rekey_config(
                session,
                provider_id=provider_id,
                old_account_id="default",
                new_account_id=target,
                on_collision=on_collision,
            )
        except RekeyCollisionError as exc:
            raise ValueError(str(exc)) from exc
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Rekeyed {provider_id}/default onto {target}",
                counts={
                    "credential_tags_moved": result.credential_tags_moved,
                    "webhook_configs_moved": result.webhook_configs_moved,
                    "webhook_configs_dropped_duplicate": result.webhook_configs_dropped_duplicate,
                    "gauge_series_merged": result.gauge_series.merged,
                    "gauge_series_retagged": result.gauge_series.retagged,
                },
            ),
            hooks,
        )
