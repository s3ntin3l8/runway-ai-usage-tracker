"""Data Health ``misidentified_gauge_series`` check — a ``latest_usage`` /
``quota_snapshots`` series whose ``(provider_id, account_id)`` has no
supporting evidence.

These arise from transient collector mis-identification (e.g. an email label
leaking across providers that short-circuits identity resolution on an unconfigured
account). Unlike ``orphan_gauge_series`` — which conservatively waits 30 days
because an active server account or unconfigured account may simply be in use —
the strict 5-criterion conjunction here guarantees safety without an arbitrary
time delay:

1. The account is a specific, non-default account (``account_id not in ("default", "")``).
   Default/server-configured accounts are never flagged.
2. No ``usage_events`` exist for this ``(provider_id, account_id)``.
3. No ``credential_sources`` are associated with this ``(provider_id, account_id)``.
4. No ``credential_tags`` point to this ``(provider_id, account_id)``.
5. No ``provider_configs`` or ``provider_account_labels`` exist for this ``(provider_id, account_id)``.

An account satisfying all five criteria has provably never been a real account,
and its gauge data (including contributions) can be deleted immediately.

Exception — login-keyed providers (GitHub): an *email-shaped* series is almost
always the real account re-keyed by its email label, not a phantom. When exactly
one evidenced non-email account of the same provider has gauge data, the finding
offers (and defaults to) a merge into it, which keeps the real history.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, or_
from sqlmodel import Session, col, select

from app.models.db import (
    CredentialSource,
    CredentialTag,
    LatestUsage,
    LatestUsageContribution,
    ProviderAccountLabel,
    ProviderConfig,
    QuotaSnapshot,
    UsageEvent,
)
from app.services.account_identity import EMAIL_RE, LOGIN_KEYED_PROVIDERS
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

_KEY_SEP = "::"


def _key(provider_id: str, account_id: str) -> str:
    return f"{provider_id}{_KEY_SEP}{account_id}"


def _parse_key(group_key: str) -> tuple[str, str]:
    provider_id, _, account_id = group_key.partition(_KEY_SEP)
    return provider_id, account_id


def _fetch_evidence_pairs(session: Session) -> set[tuple[str, str]]:
    """Bulk fetch all (provider_id, account_id) pairs with evidence across all 5 supporting tables."""
    events_pairs = {
        (row[0], row[1])
        for row in session.execute(select(UsageEvent.provider_id, UsageEvent.account_id).distinct())
    }
    sources_pairs = {
        (row[0], row[1])
        for row in session.execute(
            select(CredentialSource.provider_id, CredentialSource.account_id).distinct()
        )
    }
    tags_pairs = {
        (row[0], row[1])
        for row in session.execute(
            select(
                func.coalesce(CredentialTag.target_provider_id, CredentialTag.provider_id),
                CredentialTag.account_id,
            ).distinct()
        )
    }
    config_pairs = {
        (row[0], row[1])
        for row in session.execute(
            select(ProviderConfig.provider_id, ProviderConfig.account_id).distinct()
        )
    }
    label_pairs = {
        (row[0], row[1])
        for row in session.execute(
            select(ProviderAccountLabel.provider_id, ProviderAccountLabel.account_id).distinct()
        )
    }
    return events_pairs | sources_pairs | tags_pairs | config_pairs | label_pairs


def _has_evidence(
    session: Session,
    provider_id: str,
    account_id: str,
    *,
    evidence_pairs: set[tuple[str, str]] | None = None,
) -> bool:
    """Return True when at least one piece of supporting evidence exists for
    this (provider_id, account_id).  Any positive result means the account
    should NOT be treated as misidentified.
    """
    if not account_id or account_id == "default":
        return True

    if evidence_pairs is not None:
        return (provider_id, account_id) in evidence_pairs

    # usage_events — the event-sourced truth
    if (
        session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                col(UsageEvent.provider_id) == provider_id,
                col(UsageEvent.account_id) == account_id,
            )
        ).scalar_one()
        > 0
    ):
        return True

    # credential_sources — a sidecar actively supplies credentials for this account
    if (
        session.execute(
            select(func.count())
            .select_from(CredentialSource)
            .where(
                col(CredentialSource.provider_id) == provider_id,
                col(CredentialSource.account_id) == account_id,
            )
        ).scalar_one()
        > 0
    ):
        return True

    # credential_tags — a credential has been explicitly tagged to this account
    if (
        session.execute(
            select(func.count())
            .select_from(CredentialTag)
            .where(
                or_(
                    col(CredentialTag.target_provider_id) == provider_id,
                    and_(
                        col(CredentialTag.provider_id) == provider_id,
                        col(CredentialTag.target_provider_id).is_(None),
                    ),
                ),
                col(CredentialTag.account_id) == account_id,
            )
        ).scalar_one()
        > 0
    ):
        return True

    # provider_configs — the account has been explicitly configured in the database
    if (
        session.exec(
            select(ProviderConfig).where(
                col(ProviderConfig.provider_id) == provider_id,
                col(ProviderConfig.account_id) == account_id,
            )
        ).first()
        is not None
    ):
        return True

    # provider_account_labels — the account has an explicit override label
    if (
        session.exec(
            select(ProviderAccountLabel).where(
                col(ProviderAccountLabel.provider_id) == provider_id,
                col(ProviderAccountLabel.account_id) == account_id,
            )
        ).first()
        is not None
    ):
        return True

    return False


def _merge_candidates(
    provider_id: str,
    account_id: str,
    gauge_pairs: set[tuple[str, str]],
    evidence_pairs: set[tuple[str, str]],
) -> list[str]:
    """Evidenced, non-email accounts of a login-keyed provider that an email-keyed
    series could belong to. Empty for any other provider/account shape."""
    if provider_id not in LOGIN_KEYED_PROVIDERS or not EMAIL_RE.match(account_id):
        return []
    return sorted(
        acc
        for prov, acc in gauge_pairs
        if prov == provider_id
        and acc not in (account_id, "default")
        and not EMAIL_RE.match(acc)
        and (prov, acc) in evidence_pairs
    )


def _count_latest(session: Session, provider_id: str, account_id: str) -> int:
    return session.execute(
        select(func.count())
        .select_from(LatestUsage)
        .where(
            col(LatestUsage.provider_id) == provider_id,
            col(LatestUsage.account_id) == account_id,
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


def _count_contributions(session: Session, provider_id: str, account_id: str) -> int:
    return session.execute(
        select(func.count())
        .select_from(LatestUsageContribution)
        .where(
            col(LatestUsageContribution.provider_id) == provider_id,
            col(LatestUsageContribution.account_id) == account_id,
        )
    ).scalar_one()


class MisidentifiedGaugeSeriesCheck(Check):
    id = "misidentified_gauge_series"
    title = "Gauge data has no supporting evidence"
    description = (
        "Quota history exists for an account with no usage events, credentials, or config. "
        "This usually means a collector briefly ran under a misidentified account_id "
        "(e.g. an email label leaked from another provider's token cache)."
    )
    impact = (
        "The phantom account appears as an auto-discovered provider card in the dashboard "
        "even though it has never been a real account."
    )
    recommended_action = (
        "Delete the gauge data. There is no real usage history attached to this account."
    )
    severity = Severity.ERROR

    def detect(self, session: Session, **_kwargs: Any) -> CheckReport:  # noqa: ANN401
        # Pre-aggregate gauge counts per (provider_id, account_id) in two batched queries
        # rather than querying per pair (avoids N+1 query amplification).
        lu_counts = {
            (row[0], row[1]): row[2]
            for row in session.execute(
                select(
                    LatestUsage.provider_id,
                    LatestUsage.account_id,
                    func.count(),
                ).group_by(LatestUsage.provider_id, LatestUsage.account_id)
            )
        }
        snap_counts = {
            (row[0], row[1]): row[2]
            for row in session.execute(
                select(
                    QuotaSnapshot.provider_id,
                    QuotaSnapshot.account_id,
                    func.count(),
                ).group_by(QuotaSnapshot.provider_id, QuotaSnapshot.account_id)
            )
        }
        gauge_pairs = set(lu_counts.keys()) | set(snap_counts.keys())

        # Bulk fetch pairs with evidence across all 5 supporting tables.
        evidence_pairs = _fetch_evidence_pairs(session)

        groups: list[FindingGroup] = []
        for provider_id, account_id in sorted(gauge_pairs):
            # Universal invariant: "default" accounts represent server-configured
            # credentials (.env or file without DB rows) and are never misidentified.
            if _has_evidence(session, provider_id, account_id, evidence_pairs=evidence_pairs):
                continue

            lu_count = lu_counts.get((provider_id, account_id), 0)
            qs_count = snap_counts.get((provider_id, account_id), 0)
            candidates = _merge_candidates(provider_id, account_id, gauge_pairs, evidence_pairs)
            suggested = candidates[0] if len(candidates) == 1 else None
            groups.append(
                FindingGroup(
                    key=_key(provider_id, account_id),
                    label=(
                        f"{provider_id}/{account_id}: email-keyed duplicate of {suggested}"
                        " (still being written — the collector re-keyed the account)"
                        if suggested
                        else f"{provider_id}/{account_id}: gauge data with no supporting evidence"
                    ),
                    count=lu_count + qs_count,
                    fixable=True,
                    params=[
                        ParamSpec(
                            name="action",
                            label="Action",
                            options=["merge", "delete"] if suggested else ["delete", "merge"],
                        ),
                        ParamSpec(
                            name="target",
                            label="Merge target (merge only)",
                            required=False,
                            options=candidates or None,
                        ),
                    ]
                    if candidates
                    else [],
                    detail={"candidates": candidates, "suggested_target": suggested}
                    if suggested
                    else {},
                    samples=[
                        Finding(
                            label=f"{provider_id}/{account_id}",
                            detail={
                                "provider_id": provider_id,
                                "account_id": account_id,
                                "latest_usage_rows": lu_count,
                                "quota_snapshots_rows": qs_count,
                            },
                        )
                    ],
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def _resolve_action(
        self,
        session: Session,
        provider_id: str,
        account_id: str,
        params: dict[str, Any],
        evidence_pairs: set[tuple[str, str]],
    ) -> tuple[str, str | None]:
        """Return ``(action, target)``. Defaults to merging when there is exactly
        one plausible real account (keeps history); otherwise deleting."""
        gauge_pairs = {
            (row[0], row[1])
            for row in session.execute(
                select(LatestUsage.provider_id, LatestUsage.account_id).distinct()
            )
        } | {
            (row[0], row[1])
            for row in session.execute(
                select(QuotaSnapshot.provider_id, QuotaSnapshot.account_id).distinct()
            )
        }
        candidates = _merge_candidates(provider_id, account_id, gauge_pairs, evidence_pairs)
        action = params.get("action") or ("merge" if len(candidates) == 1 else "delete")
        if action == "merge":
            target = params.get("target") or (candidates[0] if len(candidates) == 1 else None)
            if not target or target not in candidates:
                raise ValueError(f"merge requires a target in {candidates}")
            return "merge", str(target)
        if action == "delete":
            return "delete", None
        raise ValueError(f"unknown action {action!r}; expected 'delete' or 'merge'")

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        provider_id, account_id = _parse_key(group_key)
        evidence_pairs = _fetch_evidence_pairs(session)
        if _has_evidence(session, provider_id, account_id, evidence_pairs=evidence_pairs):
            raise ValueError(
                f"{provider_id}/{account_id} now has supporting evidence and is not misidentified"
            )
        action, target = self._resolve_action(
            session, provider_id, account_id, params, evidence_pairs
        )
        if action == "merge" and target:
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
                samples=[Finding(label=sample) for sample in merge_plan.samples],
            )
        confirmation_text = f"I confirm {provider_id}/{account_id} has no real usage history."
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=f"Delete misidentified gauge series for {provider_id}/{account_id}",
            counts={
                "latest_usage": _count_latest(session, provider_id, account_id),
                "quota_snapshots": _count_snapshots(session, provider_id, account_id),
                "latest_usage_contributions": _count_contributions(
                    session, provider_id, account_id
                ),
            },
            confirmation_text=confirmation_text,
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        provider_id, account_id = _parse_key(group_key)
        evidence_pairs = _fetch_evidence_pairs(session)
        if _has_evidence(session, provider_id, account_id, evidence_pairs=evidence_pairs):
            raise ValueError(
                f"{provider_id}/{account_id} now has supporting evidence and is not misidentified"
            )
        action, target = self._resolve_action(
            session, provider_id, account_id, params, evidence_pairs
        )
        if action == "merge" and target:
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
        if params.get("same_account_confirmed") is not True:
            raise ValueError(
                f"Confirmation required to delete gauge data for {provider_id}/{account_id}"
            )
        result = delete_gauge_series(session, provider_id=provider_id, account_id=account_id)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Deleted misidentified gauge series for {provider_id}/{account_id}",
                counts={
                    "latest_usage_deleted": result.latest_usage_deleted,
                    "snapshots_deleted": result.snapshots_deleted,
                    "contributions_deleted": result.contributions_deleted,
                },
            ),
            [],
        )
