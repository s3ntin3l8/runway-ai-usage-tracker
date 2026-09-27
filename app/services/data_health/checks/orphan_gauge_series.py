"""Data Health `orphan_gauge_series` check — a `latest_usage`/
`quota_snapshots` series for an account with no `provider_configs` row and
no recent event activity (D9 in the v3.0.0 prod-cleanup audit: a stray
`minimax/default` card, a `github noreply` card). Not every unconfigured
account is orphaned — one that's still posting fresh events just hasn't
been configured *yet*, so `stale_days` gates on recency, not just on a
missing config row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.db import LatestUsage, ProviderConfig, QuotaSnapshot, UsageEvent
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
from app.services.maintenance.account_merge import (
    delete_gauge_series,
    merge_gauge_series,
    plan_merge_gauge_series,
)

_STALE_DAYS_DEFAULT = 30
_KEY_SEP = "::"


def _key(provider_id: str, account_id: str) -> str:
    return f"{provider_id}{_KEY_SEP}{account_id}"


def _parse_key(group_key: str) -> tuple[str, str]:
    provider_id, _, account_id = group_key.partition(_KEY_SEP)
    return provider_id, account_id


def _pairs_with_gauge_series(session: Session) -> set[tuple[str, str]]:
    latest = {
        (r[0], r[1])
        for r in session.execute(select(LatestUsage.provider_id, LatestUsage.account_id).distinct())
    }
    snaps = {
        (r[0], r[1])
        for r in session.execute(
            select(QuotaSnapshot.provider_id, QuotaSnapshot.account_id).distinct()
        )
    }
    return latest | snaps


def _has_config(session: Session, provider_id: str, account_id: str) -> bool:
    return (
        session.exec(
            select(ProviderConfig).where(
                col(ProviderConfig.provider_id) == provider_id,
                col(ProviderConfig.account_id) == account_id,
            )
        ).first()
        is not None
    )


def _last_event_ts(session: Session, provider_id: str, account_id: str) -> datetime | None:
    return session.execute(
        select(func.max(UsageEvent.ts)).where(
            col(UsageEvent.provider_id) == provider_id, col(UsageEvent.account_id) == account_id
        )
    ).scalar_one()


def _count_latest(session: Session, provider_id: str, account_id: str) -> int:
    return session.execute(
        select(func.count())
        .select_from(LatestUsage)
        .where(
            col(LatestUsage.provider_id) == provider_id, col(LatestUsage.account_id) == account_id
        )
    ).scalar_one()


def _count_snapshots(session: Session, provider_id: str, account_id: str) -> int:
    return session.execute(
        select(func.count())
        .select_from(QuotaSnapshot)
        .where(
            col(QuotaSnapshot.provider_id) == provider_id,
            col(QuotaSnapshot.account_id) == account_id,
        )
    ).scalar_one()


class OrphanGaugeSeriesCheck(Check):
    id = "orphan_gauge_series"
    severity = Severity.WARN

    def detect(self, session: Session, *, stale_days: int = _STALE_DAYS_DEFAULT) -> CheckReport:
        cutoff = datetime.now(UTC) - timedelta(days=stale_days)
        groups: list[FindingGroup] = []
        for provider_id, account_id in sorted(_pairs_with_gauge_series(session)):
            if _has_config(session, provider_id, account_id):
                continue
            last_ts = _last_event_ts(session, provider_id, account_id)
            last_ts_naive = (
                last_ts.replace(tzinfo=UTC) if last_ts and last_ts.tzinfo is None else last_ts
            )
            if last_ts_naive is not None and last_ts_naive >= cutoff:
                continue  # still posting events — not configured yet, not orphaned
            candidates = [a for a in candidate_targets(session, provider_id) if a != account_id]
            latest_count = _count_latest(session, provider_id, account_id)
            snap_count = _count_snapshots(session, provider_id, account_id)
            groups.append(
                FindingGroup(
                    key=_key(provider_id, account_id),
                    label=f"{provider_id}/{account_id}: orphan gauge series",
                    count=latest_count + snap_count,
                    fixable=True,
                    params=[
                        ParamSpec(name="action", label="Action", options=["delete", "merge"]),
                        ParamSpec(
                            name="target",
                            label="Merge target (merge only)",
                            required=False,
                            options=candidates or None,
                        ),
                    ],
                    samples=[
                        Finding(
                            label=f"{provider_id}/{account_id}",
                            detail={
                                "provider_id": provider_id,
                                "account_id": account_id,
                                "latest_usage_rows": latest_count,
                                "quota_snapshots_rows": snap_count,
                                "last_event_ts": last_ts.isoformat() if last_ts else None,
                            },
                        )
                    ],
                    detail={
                        "candidates": candidates,
                        "last_event_ts": last_ts.isoformat() if last_ts else None,
                    },
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def _resolve_merge_target(
        self, session: Session, provider_id: str, account_id: str, params: dict[str, Any]
    ) -> str:
        candidates = [a for a in candidate_targets(session, provider_id) if a != account_id]
        target = params.get("target")
        if not target or target not in candidates:
            raise ValueError(f"merge requires a target in {candidates}")
        return str(target)

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        provider_id, account_id = _parse_key(group_key)
        action = params.get("action", "delete")
        if action == "merge":
            target = self._resolve_merge_target(session, provider_id, account_id, params)
            merge_plan = plan_merge_gauge_series(
                session, provider_id=provider_id, source=account_id, target=target
            )
            return FixPlan(
                check_id=self.id,
                group_key=group_key,
                summary=f"Merge {provider_id}/{account_id} into {target}",
                counts={
                    "merged": merge_plan.merged,
                    "retagged": merge_plan.retagged,
                    "snapshots_retagged": merge_plan.snapshots_retagged,
                    "snapshots_collided": merge_plan.snapshots_collided,
                },
            )
        if action == "delete":
            return FixPlan(
                check_id=self.id,
                group_key=group_key,
                summary=f"Delete gauge series for {provider_id}/{account_id}",
                counts={
                    "latest_usage": _count_latest(session, provider_id, account_id),
                    "quota_snapshots": _count_snapshots(session, provider_id, account_id),
                },
            )
        raise ValueError(f"unknown action {action!r}; expected 'delete' or 'merge'")

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        provider_id, account_id = _parse_key(group_key)
        action = params.get("action", "delete")
        if action == "merge":
            target = self._resolve_merge_target(session, provider_id, account_id, params)
            merge_result = merge_gauge_series(
                session, provider_id=provider_id, source=account_id, target=target
            )
            return (
                FixResult(
                    check_id=self.id,
                    group_key=group_key,
                    summary=f"Merged {provider_id}/{account_id} into {target}",
                    counts={
                        "merged": merge_result.merged,
                        "retagged": merge_result.retagged,
                        "snapshots_retagged": merge_result.snapshots_retagged,
                        "snapshots_collided": merge_result.snapshots_collided,
                    },
                ),
                [],
            )
        if action == "delete":
            delete_result = delete_gauge_series(
                session, provider_id=provider_id, account_id=account_id
            )
            return (
                FixResult(
                    check_id=self.id,
                    group_key=group_key,
                    summary=f"Deleted gauge series for {provider_id}/{account_id}",
                    counts={
                        "latest_usage_deleted": delete_result.latest_usage_deleted,
                        "snapshots_deleted": delete_result.snapshots_deleted,
                    },
                ),
                [],
            )
        raise ValueError(f"unknown action {action!r}; expected 'delete' or 'merge'")
