"""Merge or delete a stray account's gauge series (`latest_usage` +
`quota_snapshots`) — the logic behind `scripts/merge_gemini_default_account.py`
and the Data Health `orphan_gauge_series` fixer.

Scope is deliberately narrower than a full account rekey
(`config_rekey.py`): `usage_events` / `usage_period_rollup` are untouched
here — this only folds or drops the *live gauge* series (the dashboard
card and its `%`-history), for an account that never had its own event
history (a stray `latest_usage`/`quota_snapshots` twin left behind by an
account-identity fix, not a real second account).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlmodel import Session, col, delete, select, text

from app.models.db import LatestUsage, QuotaSnapshot
from app.services.accumulator import merge_card_json

# Identity fields carried inside card_json that must NOT leak from the
# source card into the target card during a quota merge.
_IDENTITY_KEYS = ("account_id", "account_label")


def _grain(row: LatestUsage | QuotaSnapshot) -> tuple:
    return (row.window_type, row.variant, row.model_id)


def _count_colliding_snapshots(session: Session, provider_id: str, source: str, target: str) -> int:
    """Count source-side quota_snapshots whose (window_type, variant,
    model_id, ts) already exists under target — the unique constraint's
    key. A correlated EXISTS, not a Python set: a stray account's snapshot
    history can run into the tens of thousands of rows."""
    return session.execute(
        text(
            "SELECT COUNT(*) FROM quota_snapshots s WHERE s.provider_id = :p "
            "AND s.account_id = :source AND EXISTS ("
            "  SELECT 1 FROM quota_snapshots t WHERE t.provider_id = s.provider_id "
            "  AND t.account_id = :target AND t.window_type = s.window_type "
            "  AND t.variant = s.variant AND t.model_id = s.model_id AND t.ts = s.ts"
            ")"
        ),
        {"p": provider_id, "source": source, "target": target},
    ).scalar_one()


def _count_snapshots(session: Session, provider_id: str, account_id: str) -> int:
    return session.execute(
        text("SELECT COUNT(*) FROM quota_snapshots WHERE provider_id = :p AND account_id = :a"),
        {"p": provider_id, "a": account_id},
    ).scalar_one()


@dataclass
class MergePlan:
    merged: int = 0  # source card folded into an existing target card
    retagged: int = 0  # source card moved (no colliding target card)
    snapshots_retagged: int = 0
    snapshots_collided: int = 0  # exact grain+ts already exists under target — dropped
    samples: list[str] = field(default_factory=list)


def plan_merge_gauge_series(
    session: Session, *, provider_id: str, source: str, target: str
) -> MergePlan:
    """Read-only preview of `merge_gauge_series`."""
    plan = MergePlan()
    rows = session.exec(select(LatestUsage).where(LatestUsage.provider_id == provider_id)).all()
    by_account_grain: dict[str, dict[tuple, LatestUsage]] = {}
    for r in rows:
        by_account_grain.setdefault(r.account_id, {})[_grain(r)] = r
    src_rows = by_account_grain.get(source, {})
    tgt_rows = by_account_grain.get(target, {})
    for grain, _src in sorted(src_rows.items()):
        if grain in tgt_rows:
            plan.merged += 1
            plan.samples.append(f"merge {grain} -> existing {target} card")
        else:
            plan.retagged += 1
            plan.samples.append(f"retag {grain} -> {target} (no existing card)")

    total_snaps = _count_snapshots(session, provider_id, source)
    collided = _count_colliding_snapshots(session, provider_id, source, target)
    plan.snapshots_collided = collided
    plan.snapshots_retagged = total_snaps - collided
    return plan


def merge_gauge_series(
    session: Session, *, provider_id: str, source: str, target: str
) -> MergePlan:
    """Fold `source`'s latest_usage/quota_snapshots into `target`. Commits.

    A `source` card merges into a same-grain `target` card via
    `merge_card_json` (preserving the target's enrichment); with no
    colliding grain, the source card is retagged onto `target` in place.
    quota_snapshots are bulk-retagged, dropping any exact grain+ts
    collision (the unique constraint's key).
    """
    result = MergePlan()
    rows = session.exec(select(LatestUsage).where(LatestUsage.provider_id == provider_id)).all()
    by_account_grain: dict[str, dict[tuple, LatestUsage]] = {}
    for r in rows:
        by_account_grain.setdefault(r.account_id, {})[_grain(r)] = r
    src_rows = by_account_grain.get(source, {})
    tgt_rows = by_account_grain.get(target, {})

    for grain, src in sorted(src_rows.items()):
        tgt = tgt_rows.get(grain)
        if tgt is not None:
            incoming = json.loads(src.card_json)
            for k in _IDENTITY_KEYS:
                incoming.pop(k, None)
            tgt.card_json = merge_card_json(tgt.card_json, incoming)
            if src.updated_at and (not tgt.updated_at or src.updated_at > tgt.updated_at):
                tgt.updated_at = src.updated_at
            session.add(tgt)
            session.delete(src)
            result.merged += 1
        else:
            card = json.loads(src.card_json)
            card["account_id"] = target
            card["account_label"] = target
            src.account_id = target
            src.card_json = json.dumps(card)
            session.add(src)
            result.retagged += 1

    # Bulk retag; SQLite silently skips a row whose (provider_id, account_id,
    # window_type, variant, model_id, ts) would collide with an existing
    # target row (the unique constraint), leaving it still under `source`.
    update_result = session.execute(
        text(
            "UPDATE OR IGNORE quota_snapshots SET account_id = :target "
            "WHERE provider_id = :p AND account_id = :source"
        ),
        {"target": target, "p": provider_id, "source": source},
    )
    result.snapshots_retagged = update_result.rowcount or 0  # type: ignore[attr-defined]
    # Whatever's left at (provider_id, source) is exactly the collisions —
    # a genuine duplicate observation, safe to drop.
    delete_result = session.execute(
        text("DELETE FROM quota_snapshots WHERE provider_id = :p AND account_id = :source"),
        {"p": provider_id, "source": source},
    )
    result.snapshots_collided = delete_result.rowcount or 0  # type: ignore[attr-defined]

    session.commit()
    return result


@dataclass
class DeleteResult:
    latest_usage_deleted: int = 0
    snapshots_deleted: int = 0


def delete_gauge_series(session: Session, *, provider_id: str, account_id: str) -> DeleteResult:
    """Drop every latest_usage/quota_snapshots row for (provider_id,
    account_id) outright — the Data Health `orphan_gauge_series` fixer's
    "delete" action, for a series with no plausible merge target. Bulk
    SQL, not a per-row loop — a stray account's quota_snapshots history can
    run into the tens of thousands of rows. Commits.
    """
    latest_result = session.exec(
        delete(LatestUsage).where(
            col(LatestUsage.provider_id) == provider_id, col(LatestUsage.account_id) == account_id
        )
    )
    snapshots_result = session.exec(
        delete(QuotaSnapshot).where(
            col(QuotaSnapshot.provider_id) == provider_id,
            col(QuotaSnapshot.account_id) == account_id,
        )
    )
    session.commit()
    return DeleteResult(
        latest_usage_deleted=latest_result.rowcount or 0,  # type: ignore[attr-defined]
        snapshots_deleted=snapshots_result.rowcount or 0,  # type: ignore[attr-defined]
    )
