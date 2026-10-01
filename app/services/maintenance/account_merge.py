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
from datetime import UTC, datetime

from sqlmodel import Session, col, select, text

from app.models.db import LatestUsage, LatestUsageContribution, QuotaSnapshot
from app.services.accumulator import merge_card_json
from app.services.maintenance._chunked_sql import chunked_delete

# Identity fields carried inside card_json that must NOT leak from the
# source card into the target card during a quota merge.
_IDENTITY_KEYS = ("account_id", "account_label")

_SNAPSHOT_BATCH = 5000


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
    contributions_merged: int = 0
    contributions_retagged: int = 0
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

    contributions = session.exec(
        select(LatestUsageContribution).where(
            LatestUsageContribution.provider_id == provider_id,
            col(LatestUsageContribution.account_id).in_([source, target]),
        )
    ).all()
    target_contribution_keys = {
        (row.source_id, row.window_type, row.variant, row.model_id)
        for row in contributions
        if row.account_id == target
    }
    for row in contributions:
        if row.account_id != source:
            continue
        key = (row.source_id, row.window_type, row.variant, row.model_id)
        if key in target_contribution_keys:
            plan.contributions_merged += 1
        else:
            plan.contributions_retagged += 1
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

    # Contributions are the inputs to the merged LatestUsage read model.
    # Retag non-colliding rows and discard a duplicate source contribution;
    # the target LatestUsage card above already folded its visible payload.
    contributions = session.exec(
        select(LatestUsageContribution).where(
            LatestUsageContribution.provider_id == provider_id,
            LatestUsageContribution.account_id == source,
        )
    ).all()
    for contribution in contributions:
        target_contribution = session.exec(
            select(LatestUsageContribution.id).where(
                LatestUsageContribution.provider_id == provider_id,
                LatestUsageContribution.account_id == target,
                LatestUsageContribution.source_id == contribution.source_id,
                LatestUsageContribution.window_type == contribution.window_type,
                LatestUsageContribution.variant == contribution.variant,
                LatestUsageContribution.model_id == contribution.model_id,
            )
        ).first()
        if target_contribution is not None:
            target_row = session.get(LatestUsageContribution, target_contribution)
            if target_row is not None:
                incoming = json.loads(contribution.card_json)
                for key in _IDENTITY_KEYS:
                    incoming.pop(key, None)
                target_row.card_json = merge_card_json(target_row.card_json, incoming)
                incoming_updated_at = contribution.updated_at or datetime.now(UTC)
                if not target_row.updated_at or incoming_updated_at > target_row.updated_at:
                    target_row.updated_at = incoming_updated_at
                session.add(target_row)
                session.delete(contribution)
                result.contributions_merged += 1
                continue

        # If a concurrent cleanup removed the target contribution after the
        # lookup, preserve the source by retagging it instead of dropping it.
        card = json.loads(contribution.card_json)
        card["account_id"] = target
        card["account_label"] = target
        contribution.account_id = target
        contribution.card_json = json.dumps(card)
        session.add(contribution)
        result.contributions_retagged += 1

    session.commit()

    retagged, collided = _chunked_retag_snapshots(session, provider_id, source, target)
    result.snapshots_retagged = retagged
    result.snapshots_collided = collided
    return result


def _chunked_retag_snapshots(
    session: Session,
    provider_id: str,
    source: str,
    target: str,
    *,
    batch_size: int = _SNAPSHOT_BATCH,
) -> tuple[int, int]:
    """Bulk-retag `quota_snapshots` from `source` to `target`, `batch_size`
    rows at a time by an `id` cursor — a stray account's snapshot history can
    run into the tens of thousands of rows, and this must never hold
    SQLite's writer lock for one giant transaction.

    Advances by `id` rather than re-querying `WHERE account_id = :source`
    each batch: `UPDATE OR IGNORE` can leave a colliding row's account_id
    unchanged, and re-selecting the same WHERE would re-match — and retry
    forever — the exact rows a batch just failed to move.
    """
    retagged = 0
    collided = 0
    cursor = 0
    while True:
        ids = [
            row[0]
            for row in session.execute(
                text(
                    "SELECT id FROM quota_snapshots WHERE provider_id = :p "
                    "AND account_id = :source AND id > :cursor ORDER BY id LIMIT :n"
                ),
                {"p": provider_id, "source": source, "cursor": cursor, "n": batch_size},
            )
        ]
        if not ids:
            break
        placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
        id_params = {f"id{i}": v for i, v in enumerate(ids)}

        # A colliding row's (provider_id, account_id, window_type, variant,
        # model_id, ts) already exists under target — OR IGNORE leaves it
        # untouched under source, still 1:1 with this batch's ids.
        # placeholders is a fixed list of bound-parameter names (:id0, :id1,
        # ...), never row data — id_params below binds the real values.
        update_sql = f"UPDATE OR IGNORE quota_snapshots SET account_id = :target WHERE id IN ({placeholders})"  # noqa: S608
        update_result = session.execute(text(update_sql), {**id_params, "target": target})
        session.commit()
        moved = update_result.rowcount or 0  # type: ignore[attr-defined]
        retagged += moved

        if moved < len(ids):
            # Whatever's still under `source` in this batch is exactly the
            # collisions — a genuine duplicate observation, safe to drop.
            delete_sql = (
                f"DELETE FROM quota_snapshots WHERE account_id = :source AND id IN ({placeholders})"  # noqa: S608
            )
            delete_result = session.execute(text(delete_sql), {**id_params, "source": source})
            session.commit()
            collided += delete_result.rowcount or 0  # type: ignore[attr-defined]

        cursor = ids[-1]
        if len(ids) < batch_size:
            break
    return retagged, collided


@dataclass
class DeleteResult:
    latest_usage_deleted: int = 0
    snapshots_deleted: int = 0
    contributions_deleted: int = 0


def delete_gauge_series(session: Session, *, provider_id: str, account_id: str) -> DeleteResult:
    """Drop every latest_usage/quota_snapshots/latest_usage_contributions row for
    (provider_id, account_id) outright — the Data Health `orphan_gauge_series` and
    `misidentified_gauge_series` fixer's "delete" action, for a series with no
    plausible merge target. Chunked (`_chunked_sql`) — a stray account's
    quota_snapshots history can run into the tens of thousands of rows.
    """
    latest_deleted = chunked_delete(
        session,
        LatestUsage,
        [col(LatestUsage.provider_id) == provider_id, col(LatestUsage.account_id) == account_id],
    )
    snapshots_deleted = chunked_delete(
        session,
        QuotaSnapshot,
        [
            col(QuotaSnapshot.provider_id) == provider_id,
            col(QuotaSnapshot.account_id) == account_id,
        ],
    )
    contributions_deleted = chunked_delete(
        session,
        LatestUsageContribution,
        [
            col(LatestUsageContribution.provider_id) == provider_id,
            col(LatestUsageContribution.account_id) == account_id,
        ],
    )
    return DeleteResult(
        latest_usage_deleted=latest_deleted,
        snapshots_deleted=snapshots_deleted,
        contributions_deleted=contributions_deleted,
    )
