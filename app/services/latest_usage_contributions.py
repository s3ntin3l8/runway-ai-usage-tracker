# app/services/latest_usage_contributions.py
"""Source rows behind one merged LatestUsage card.

Moved out of accumulator.py. Public entry points are re-exported there so
existing imports keep working.
"""

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, select

from app.core.utils import with_failing_prefix


def _latest_usage_sidecar_id(contributions: Iterable[Any], *, provider_id: str) -> str:
    """Select the materialized owner from contributions ordered oldest first."""
    from app.services.accumulator import parse_source_id

    latest_owner = "local"
    latest_fresh_sidecar_id: str | None = None
    has_fresh_server_source = False

    for contribution in contributions:
        source_kind, source_scope, source_provider = parse_source_id(contribution.source_id)
        if source_provider != provider_id:
            continue
        latest_owner = source_scope if source_kind == "sidecar" else "local"
        if json.loads(contribution.card_json or "{}").get("stale") is True:
            continue
        if source_kind == "server":
            has_fresh_server_source = True
        elif source_kind == "sidecar":
            latest_fresh_sidecar_id = source_scope

    if has_fresh_server_source:
        return "local"
    return latest_fresh_sidecar_id or latest_owner


def rebuild_latest_usage_slot(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str,
    variant: str,
    model_id: str,
) -> None:
    """Materialize one logical card from the sources that still report it."""
    from app.models.db import LatestUsage, LatestUsageContribution
    from app.services.accumulator import merge_card_json

    contributions = session.exec(
        select(LatestUsageContribution)
        .where(
            LatestUsageContribution.provider_id == provider_id,
            LatestUsageContribution.account_id == account_id,
            LatestUsageContribution.window_type == window_type,
            LatestUsageContribution.variant == variant,
            LatestUsageContribution.model_id == model_id,
        )
        .order_by(
            col(LatestUsageContribution.updated_at),
            col(LatestUsageContribution.source_id),
        )
    ).all()
    row = session.exec(
        select(LatestUsage).where(
            LatestUsage.provider_id == provider_id,
            LatestUsage.account_id == account_id,
            LatestUsage.window_type == window_type,
            LatestUsage.variant == variant,
            LatestUsage.model_id == model_id,
        )
    ).first()
    if not contributions:
        if row:
            session.delete(row)
        return

    merged: str | None = None
    stale = True
    latest_at = None
    for contribution in contributions:
        payload = json.loads(contribution.card_json or "{}")
        merged = merge_card_json(merged, payload)
        stale = stale and payload.get("stale") is True
        latest_at = contribution.updated_at
    sidecar_id = _latest_usage_sidecar_id(contributions, provider_id=provider_id)
    data = json.loads(merged or "{}")
    if stale:
        data["stale"] = True
        data["collection_failing"] = True
    else:
        data.pop("stale", None)
        data.pop("collection_failing", None)
    materialized = json.dumps(data)
    if row:
        row.card_json = materialized
        row.sidecar_id = sidecar_id
        row.updated_at = latest_at or datetime.now(UTC)
    else:
        session.add(
            LatestUsage(
                provider_id=provider_id,
                account_id=account_id,
                sidecar_id=sidecar_id,
                window_type=window_type,
                variant=variant,
                model_id=model_id,
                card_json=materialized,
                updated_at=latest_at or datetime.now(UTC),
            )
        )


def upsert_contribution(
    session: Session, *, source_id: str, card: Any, account_id: str, card_json: dict
) -> None:
    from app.models.db import LatestUsageContribution
    from app.services.accumulator import merge_card_json

    row = session.exec(
        select(LatestUsageContribution).where(
            LatestUsageContribution.provider_id == card.provider_id,
            LatestUsageContribution.account_id == account_id,
            LatestUsageContribution.source_id == source_id,
            LatestUsageContribution.window_type == card.window_type,
            LatestUsageContribution.variant == (card.variant or "default"),
            LatestUsageContribution.model_id == (card.model_id or ""),
        )
    ).first()
    encoded = json.dumps(card_json)
    now = datetime.now(UTC)
    if row:
        previous = json.loads(row.card_json or "{}")
        # A fresh successful card can recover from a stale source even when
        # that card has no quota fields (for example, a token-count card).
        previous.pop("stale", None)
        previous.pop("collection_failing", None)
        row.card_json = merge_card_json(json.dumps(previous), card_json)
        row.updated_at = now
    else:
        session.add(
            LatestUsageContribution(
                provider_id=card.provider_id,
                account_id=account_id,
                source_id=source_id,
                window_type=card.window_type,
                variant=card.variant or "default",
                model_id=card.model_id or "",
                card_json=encoded,
                updated_at=now,
            )
        )


def backfill_latest_usage_contributions(session: Session) -> int:
    """Seed producer ownership from existing current cards on upgrade."""
    from app.models.db import LatestUsage, LatestUsageContribution
    from app.services.accumulator import is_error_card_json, sidecar_source_id

    rows = session.exec(select(LatestUsage)).all()
    inserted = 0
    for row in rows:
        try:
            data = json.loads(row.card_json or "{}")
        except (ValueError, TypeError):
            continue
        if is_error_card_json(row.card_json):
            continue
        # Existing rows predate source ownership and may contain merged
        # provenance. Prefer the API collector when it was one of the sources;
        # otherwise retain the sidecar that last wrote the row.
        data_sources = set(str(data.get("data_source") or "").split(","))
        if row.sidecar_id == "local" or "api" in data_sources:
            source_id = f"server:{row.provider_id}"
        else:
            source_id = sidecar_source_id(row.provider_id, row.sidecar_id or "local")
        existing = session.exec(
            select(LatestUsageContribution).where(
                LatestUsageContribution.provider_id == row.provider_id,
                LatestUsageContribution.account_id == row.account_id,
                LatestUsageContribution.source_id == source_id,
                LatestUsageContribution.window_type == row.window_type,
                LatestUsageContribution.variant == row.variant,
                LatestUsageContribution.model_id == row.model_id,
            )
        ).first()
        if existing:
            continue
        session.add(
            LatestUsageContribution(
                provider_id=row.provider_id,
                account_id=row.account_id,
                source_id=source_id,
                window_type=row.window_type,
                variant=row.variant,
                model_id=row.model_id,
                card_json=row.card_json,
                updated_at=row.updated_at,
            )
        )
        inserted += 1
    return inserted


def prune_orphan_latest_usage_contributions(session: Session) -> int:
    """Drop source rows whose materialized LatestUsage slot was deleted."""
    from app.models.db import LatestUsage, LatestUsageContribution

    live_slots = {
        (row.provider_id, row.account_id, row.window_type, row.variant, row.model_id)
        for row in session.exec(select(LatestUsage)).all()
    }
    contributions = session.exec(select(LatestUsageContribution)).all()
    removed = 0
    for row in contributions:
        key = (row.provider_id, row.account_id, row.window_type, row.variant, row.model_id)
        if key not in live_slots:
            session.delete(row)
            removed += 1
    return removed


def reconcile_latest_usage_snapshot(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    source_id: str,
    reported_keys: set[tuple[str, str, str]],
) -> int:
    """Remove this source's cards absent from a complete provider/account report."""
    from app.models.db import LatestUsageContribution

    current = session.exec(
        select(LatestUsageContribution).where(
            LatestUsageContribution.provider_id == provider_id,
            LatestUsageContribution.account_id == account_id,
            LatestUsageContribution.source_id == source_id,
        )
    ).all()
    removed = 0
    touched: set[tuple[str, str, str]] = set()
    for row in current:
        key = (row.window_type, row.variant, row.model_id)
        if key in reported_keys:
            continue
        session.delete(row)
        touched.add(key)
        removed += 1
    for window_type, variant, model_id in touched:
        rebuild_latest_usage_slot(
            session,
            provider_id=provider_id,
            account_id=account_id,
            window_type=window_type,
            variant=variant,
            model_id=model_id,
        )
    return removed


def mark_latest_usage_source_stale(
    session: Session,
    *,
    provider_id: str,
    source_id: str,
    account_id: str | None = None,
    stale_after_seconds: int = 3600,
    reason: str | None = None,
) -> int:
    """Mark last-good source cards stale after sustained collection failure.

    ``reason`` says why collection is failing; it is shown in the card's detail
    prefix and replaced (never stacked) when a later failure has a new reason.

    ``account_id`` scopes the operation when a server account is no longer
    active, while ``None`` preserves the provider-wide failure behavior.
    """
    from app.models.db import LatestUsageContribution

    statement = select(LatestUsageContribution).where(
        LatestUsageContribution.provider_id == provider_id,
        LatestUsageContribution.source_id == source_id,
    )
    if account_id is not None:
        statement = statement.where(LatestUsageContribution.account_id == account_id)
    rows = session.exec(statement).all()
    touched = set()
    now = datetime.now(UTC)
    for row in rows:
        updated_at = row.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if (now - updated_at).total_seconds() <= stale_after_seconds:
            continue
        data = json.loads(row.card_json or "{}")
        data["stale"] = True
        data["collection_failing"] = True
        data["detail"] = with_failing_prefix(data.get("detail") or "", reason)
        row.card_json = json.dumps(data)
        touched.add((row.account_id, row.window_type, row.variant, row.model_id))
    for touched_account_id, window_type, variant, model_id in touched:
        rebuild_latest_usage_slot(
            session,
            provider_id=provider_id,
            account_id=touched_account_id,
            window_type=window_type,
            variant=variant,
            model_id=model_id,
        )
    return len(touched)
