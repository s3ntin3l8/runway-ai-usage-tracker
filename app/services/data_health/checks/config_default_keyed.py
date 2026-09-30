"""Data Health `config_default_keyed` check — a provider_configs row still
keyed `account_id="default"` whose `account_label` already carries a real
identity (D11 in the v3.0.0 prod-cleanup audit: kimi, minimax, ollama,
opencode). See `app/services/maintenance/config_rekey.py` for why this
blocks `POST /fleet/events/pending/assign`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.db import ProviderConfig, UsageEvent
from app.services.account_identity import EMAIL_RE, HASH_RE
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
from app.services.maintenance.event_reassign import (
    apply_reassign_default,
    plan_reassign_default,
)

_NOT_A_REAL_LABEL = {"default", ""}


def _is_rekey_candidate(row: ProviderConfig) -> bool:
    if row.archived:
        return False
    label = (row.account_label or "").strip()
    if label in _NOT_A_REAL_LABEL or label.lower() == row.account_id.lower():
        return False
    if row.account_id == "default":
        return True
    if bool(HASH_RE.match(row.account_id)) and bool(EMAIL_RE.match(label)):
        return True
    return False


def _suggested_target(row: ProviderConfig) -> str | None:
    label = (row.account_label or "").strip()
    if not _is_rekey_candidate(row):
        return None
    if row.account_id == "default":
        return label
    if bool(HASH_RE.match(row.account_id)):
        return label.lower()
    return None


def _find_row(
    session: Session, provider_id: str, account_id: str | None = None
) -> ProviderConfig | None:
    if account_id:
        return session.exec(
            select(ProviderConfig).where(
                col(ProviderConfig.provider_id) == provider_id,
                col(ProviderConfig.account_id) == account_id,
                col(ProviderConfig.archived).is_(False),
            )
        ).first()

    default_row = session.exec(
        select(ProviderConfig).where(
            col(ProviderConfig.provider_id) == provider_id,
            col(ProviderConfig.account_id) == "default",
            col(ProviderConfig.archived).is_(False),
        )
    ).first()
    if default_row is not None:
        return default_row

    # Intentional fallback for callers that omit account_id (or legacy group keys):
    # scan active configs for the first rekey candidate (e.g. hash-keyed row with email label).
    candidates = session.exec(
        select(ProviderConfig).where(
            col(ProviderConfig.provider_id) == provider_id,
            col(ProviderConfig.archived).is_(False),
        )
    ).all()
    for c in candidates:
        if _is_rekey_candidate(c):
            return c
    return None


class ConfigDefaultKeyedCheck(Check):
    id = "config_default_keyed"
    title = "Provider account uses a generic ID"
    description = "A saved provider configuration is keyed with a generic or hash ID even though its label identifies a specific account."
    impact = "Credentials and usage can be split across identities, and pending event assignment may be blocked."
    recommended_action = "Re-key the configuration to the identified account. Review any existing target config in the preview before confirming."
    severity = Severity.ERROR

    def detect(self, session: Session) -> CheckReport:
        rows = session.exec(
            select(ProviderConfig).where(
                col(ProviderConfig.archived).is_(False),
            )
        ).all()
        groups: list[FindingGroup] = []
        for row in rows:
            if not _is_rekey_candidate(row):
                continue
            target = _suggested_target(row)
            if target is None:
                continue
            target_row = _find_target_row(session, row.provider_id, target)
            params = [ParamSpec(name="new_account_id", label="New account id", required=True)]
            if target_row is not None and not target_row.archived:
                params.append(
                    ParamSpec(
                        name="on_collision",
                        label="Account already exists",
                        options=["archive_default"],
                    )
                )
            source_id = row.account_id
            source_display = "default" if source_id == "default" else f"{source_id[:8]}…"
            group_key = (
                f"{row.provider_id}:{source_id}" if source_id != "default" else row.provider_id
            )
            groups.append(
                FindingGroup(
                    key=group_key,
                    label=f"{row.provider_id}: {source_display} → {target}",
                    count=1,
                    fixable=target_row is None or not target_row.archived,
                    not_fixable_reason=(
                        f"{row.provider_id}/{target} already has an archived config; review that account first"
                        if target_row is not None and target_row.archived
                        else None
                    ),
                    params=params,
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
                    detail={
                        "suggested_new_account_id": target,
                        "current_account_id": row.account_id,
                    },
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=len(groups),
            groups=groups,
        )

    def _resolve_target(
        self,
        session: Session,
        provider_id: str,
        source_account_id: str | None,
        params: dict[str, Any],
    ) -> str:
        new_account_id = params.get("new_account_id")
        if new_account_id:
            target = str(new_account_id).strip()
        else:
            row = _find_row(session, provider_id, source_account_id)
            if row is None:
                raise ValueError(f"No rekeyable provider_config found for {provider_id!r}")
            suggested_target = _suggested_target(row)
            if suggested_target is None:
                raise ValueError(f"{provider_id!r} has no resolvable account_label to rekey onto")
            target = suggested_target
        if not target or target == "default":
            raise ValueError("new account id must be a non-default account id")
        return target

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        provider_id, *rest = group_key.split(":", 1)
        source_account_id = rest[0] if rest else None
        source = _find_row(session, provider_id, source_account_id)
        if source is None:
            raise ValueError(f"No rekeyable provider_config found for {provider_id!r}")
        old_account_id = source.account_id
        target = self._resolve_target(session, provider_id, source_account_id, params)
        rekey_plan = plan_rekey_config(
            session, provider_id=provider_id, old_account_id=old_account_id, new_account_id=target
        )
        if (
            rekey_plan.provider_config_exists_at_target
            and params.get("on_collision") != "archive_default"
        ):
            raise ValueError(
                "Target account already exists. Preview the archive option and confirm the identities match."
            )
        event_plan = plan_reassign_default(
            session,
            provider_id=provider_id,
            source=old_account_id,
            target=target,
        )
        target_row = _find_target_row(session, provider_id, target)
        if target_row is not None and target_row.archived:
            raise ValueError(
                f"Target config {provider_id}/{target} is archived; review it before rekeying"
            )
        samples = [
            Finding(
                label=f"{provider_id}/{old_account_id} (source)",
                detail={
                    "account_label": source.account_label,
                    "credentials_present": bool(
                        source.api_key_encrypted
                        or source.session_cookie_encrypted
                        or source.oai_sc_cookie_encrypted
                    ),
                    "archived_after_apply": rekey_plan.provider_config_exists_at_target,
                    "credentials_retained": True,
                },
            )
        ]
        confirmation_text = None
        if target_row is not None:
            recent = session.execute(
                select(func.count())
                .select_from(UsageEvent)
                .where(
                    col(UsageEvent.provider_id) == provider_id,
                    col(UsageEvent.account_id) == target,
                    col(UsageEvent.ts) >= datetime.now(UTC) - timedelta(days=30),
                )
            ).scalar_one()
            samples.append(
                Finding(
                    label=f"{provider_id}/{target} (target)",
                    detail={
                        "account_label": target_row.account_label,
                        "config_archived": target_row.archived,
                        "credentials_present": bool(
                            target_row.api_key_encrypted
                            or target_row.session_cookie_encrypted
                            or target_row.oai_sc_cookie_encrypted
                        ),
                        "recent_usage_events_30d": recent,
                        "usage_events_to_move": event_plan.count,
                        "note": "Source credentials are retained in the archived row; usage history moves to the selected account.",
                    },
                )
            )
            identity_sources = session.execute(
                select(UsageEvent.sidecar_id, UsageEvent.attribution_source, func.count())
                .where(
                    col(UsageEvent.provider_id) == provider_id,
                    col(UsageEvent.account_id) == target,
                    col(UsageEvent.ts) >= datetime.now(UTC) - timedelta(days=30),
                )
                .group_by(UsageEvent.sidecar_id, UsageEvent.attribution_source)
                .limit(8)
            ).all()
            if identity_sources:
                samples.append(
                    Finding(
                        label="Recent account identity evidence",
                        detail={
                            "sidecar_attribution_counts": "; ".join(
                                f"{sidecar}/{source}: {count}"
                                for sidecar, source, count in identity_sources
                            ),
                        },
                    )
                )
            confirmation_text = f"I confirm {provider_id}/{old_account_id} and {provider_id}/{target} are the same provider account."
        counts = {
            "credential_tags": rekey_plan.credential_tags,
            "webhook_configs": rekey_plan.webhook_configs,
            "webhook_configs_dropped_duplicate": rekey_plan.webhook_configs_dropped_duplicate,
            "gauge_series_merged": rekey_plan.gauge_series.merged,
            "gauge_series_retagged": rekey_plan.gauge_series.retagged,
            "usage_events_to_move": event_plan.count,
            # Deprecated response key retained for data-health API consumers.
            # These events are now moved by the repair, so none remain under default.
            "usage_events_retained_on_default": 0,
        }
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Archive {provider_id}/{old_account_id} and keep {target}"
                if target_row
                else f"Rekey {provider_id}/{old_account_id} onto {target}"
            ),
            counts=counts,
            samples=samples,
            confirmation_text=confirmation_text,
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        provider_id, *rest = group_key.split(":", 1)
        source_account_id = rest[0] if rest else None
        source = _find_row(session, provider_id, source_account_id)
        if source is None:
            raise ValueError(f"No rekeyable provider_config found for {provider_id!r}")
        old_account_id = source.account_id
        target = self._resolve_target(session, provider_id, source_account_id, params)
        on_collision = params.get("on_collision", "abort")
        if on_collision not in {"abort", "archive_default"}:
            raise ValueError("on_collision must be 'abort' or 'archive_default'")
        collision_exists = plan_rekey_config(
            session, provider_id=provider_id, old_account_id=old_account_id, new_account_id=target
        ).provider_config_exists_at_target
        if on_collision == "archive_default":
            if not collision_exists:
                raise ValueError(
                    "Target account collision no longer exists; preview the current state again"
                )
            if params.get("same_account_confirmed") is not True:
                raise ValueError("Confirm that the source and target are the same provider account")
        try:
            result, hooks = apply_rekey_config(
                session,
                provider_id=provider_id,
                old_account_id=old_account_id,
                new_account_id=target,
                on_collision=on_collision,
            )
        except RekeyCollisionError as exc:
            raise ValueError(str(exc)) from exc
        event_result = apply_reassign_default(
            session,
            provider_id=provider_id,
            source=old_account_id,
            target=target,
        )
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=(
                    f"Archived {provider_id}/{old_account_id}; kept {target}"
                    if result.provider_config_archived_source
                    else f"Rekeyed {provider_id}/{old_account_id} onto {target}"
                ),
                counts={
                    "credential_tags_moved": result.credential_tags_moved,
                    "webhook_configs_moved": result.webhook_configs_moved,
                    "webhook_configs_dropped_duplicate": result.webhook_configs_dropped_duplicate,
                    "gauge_series_merged": result.gauge_series.merged,
                    "gauge_series_retagged": result.gauge_series.retagged,
                    "usage_events_moved": event_result.moved,
                    "event_rollups_rebuilt_pairs": event_result.rollups_rebuilt_pairs,
                    "event_windows_rebuilt": event_result.windows_rebuilt,
                },
            ),
            hooks,
        )


def _find_target_row(session: Session, provider_id: str, account_id: str) -> ProviderConfig | None:
    return session.exec(
        select(ProviderConfig).where(
            col(ProviderConfig.provider_id) == provider_id,
            col(ProviderConfig.account_id) == account_id,
        )
    ).first()
