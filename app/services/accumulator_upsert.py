# app/services/accumulator_upsert.py
"""Steps of the canonical LatestUsage card write.

``upsert_latest_usage`` in accumulator.py calls these in order. Savepoint
boundaries, early returns, and log messages match the previous inline body.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, delete, or_, select

from app.core.date_utils import parse_iso8601_utc
from app.services.account_merges import resolve_merged_account

# Same logger object as accumulator.py (__name__ there is this name) so
# caplog filters and log routing do not move with the split.
logger = logging.getLogger("app.services.accumulator")


@dataclass(frozen=True)
class _PreparedLatestUsage:
    """Values computed once, in the original order, and shared by later steps."""

    card: Any
    canonical_account_id: str
    sidecar_id: str
    contribution_source_id: str
    variant: str
    model_id: str
    incoming_partial: dict
    is_error: bool


def validate_card(card_dict: dict) -> Any | None:
    """Return a LimitCard, or None when the dict must not be written."""
    from app.models.schemas import LimitCard

    try:
        card = LimitCard(**card_dict)
    except Exception as e:
        logger.warning(f"upsert_latest_usage: invalid card shape — {e}")
        return None

    if not card.provider_id or not card.account_id:
        return None
    if card.data_source == "cache":
        return None
    return card


def canonicalize_account(
    session: Session,
    card: Any,
    *,
    sidecar_id_override: str | None,
    source_id: str | None,
) -> _PreparedLatestUsage:
    """Resolve the account and the fields the later steps read."""
    from app.services.account_identity import resolve_account_id
    from app.services.accumulator import _source_id

    canonical_account_id = resolve_account_id(card.provider_id, card.account_id, card.account_label)
    # A card may claim an account that was merged away: the account — and the
    # usage it reports — now belongs to the merge's destination, so the card
    # must not bring the retired account back.
    canonical_account_id = resolve_merged_account(session, card.provider_id, canonical_account_id)
    sidecar_id = sidecar_id_override or card.sidecar_id or "local"
    contribution_source_id = source_id or _source_id(card.provider_id, sidecar_id)
    variant = card.variant or "default"
    model_id = card.model_id or ""
    incoming_partial = card.model_dump(exclude_none=True)
    # Always embed the canonical account_id so the card_json grouping key
    # matches the column (fleet API groups by card_json, not by the column).
    incoming_partial["account_id"] = canonical_account_id

    is_error = (
        bool(incoming_partial.get("error_type"))
        or incoming_partial.get("data_source") == "error"
        or incoming_partial.get("remaining") == "ERR"
    )
    return _PreparedLatestUsage(
        card=card,
        canonical_account_id=canonical_account_id,
        sidecar_id=sidecar_id,
        contribution_source_id=contribution_source_id,
        variant=variant,
        model_id=model_id,
        incoming_partial=incoming_partial,
        is_error=is_error,
    )


def error_card_is_suppressed(session: Session, prepared: _PreparedLatestUsage) -> bool:
    """Return True when a failed poll must not write over a healthy slot."""
    if not prepared.is_error:
        return False

    from app.models.db import LatestUsage
    from app.services.accumulator import _is_error_card

    card = prepared.card
    canonical_account_id = prepared.canonical_account_id
    variant = prepared.variant
    model_id = prepared.model_id
    # Suppress error cards when a healthy row already covers this account slot.
    # A failed poll cycle must not write a permanent orphan row that shows up
    # as an extra fleet-strip light next to the healthy row it can't evict.
    try:
        same_account_rows = session.exec(
            select(LatestUsage).where(
                LatestUsage.provider_id == card.provider_id,
                LatestUsage.account_id == canonical_account_id,
            )
        ).all()
        if any(not _is_error_card(r.card_json) for r in same_account_rows):
            logger.debug(
                "Skipping error card for %s/%s — healthy row exists",
                card.provider_id,
                canonical_account_id,
            )
            return True
        if canonical_account_id == "default":
            # Multi-account hardening: only suppress when a real-account
            # row already exists for the SAME (provider_id, window_type,
            # variant, model_id) slot. Pre-fix logic checked provider-level
            # only, which would silently kill a legitimate default-account
            # error card for a slot that no real-account row covers.
            slot_real_row = session.exec(
                select(LatestUsage).where(
                    LatestUsage.provider_id == card.provider_id,
                    LatestUsage.account_id != "default",
                    LatestUsage.window_type == card.window_type,
                    LatestUsage.variant == variant,
                    LatestUsage.model_id == model_id,
                )
            ).first()
            if slot_real_row:
                logger.debug(
                    "Skipping default-account error card for %s/%s/%s/%s — "
                    "same-slot real account row exists",
                    card.provider_id,
                    card.window_type,
                    variant,
                    model_id,
                )
                return True
    except Exception as e:
        logger.debug("Error-suppression check failed for %s: %s", card.provider_id, e)
    return False


def close_previous_window(
    session: Session,
    *,
    existing: Any,
    card: Any,
    canonical_account_id: str,
) -> None:
    """Archive the just-closed window when reset_at has advanced.

    The private window-closer helper stays imported inside this function, the
    same way upsert_latest_usage imported it, so it is not published here.
    """
    from app.services.window_closer import _maybe_close_previous_window

    try:
        new_reset_dt = parse_iso8601_utc(card.reset_at)
        _maybe_close_previous_window(
            session,
            existing=existing,
            provider_id=card.provider_id,
            account_id=canonical_account_id,
            window_type=card.window_type,
            new_reset_at=new_reset_dt,
        )
    except Exception as exc:
        logger.debug(
            f"Window-close detection skipped for {card.provider_id}/{canonical_account_id}: {exc}"
        )


def merge_latest_usage_row(
    session: Session,
    *,
    existing: Any,
    prepared: _PreparedLatestUsage,
) -> None:
    """Merge the card into the current slot, or insert the slot."""
    from app.models.db import LatestUsage
    from app.services.accumulator import merge_card_json

    card = prepared.card
    if existing:
        existing.card_json = merge_card_json(existing.card_json, prepared.incoming_partial)
        existing.sidecar_id = prepared.sidecar_id
        existing.updated_at = datetime.now(UTC)
    else:
        session.add(
            LatestUsage(
                provider_id=card.provider_id,
                account_id=prepared.canonical_account_id,
                sidecar_id=prepared.sidecar_id,
                window_type=card.window_type,
                variant=prepared.variant,
                model_id=prepared.model_id,
                card_json=merge_card_json(None, prepared.incoming_partial),
            )
        )


def savepoint_upsert(session: Session, prepared: _PreparedLatestUsage) -> bool:
    """Merge the card inside one savepoint. False means the caller must stop."""
    from app.models.db import LatestUsage

    card = prepared.card
    canonical_account_id = prepared.canonical_account_id
    variant = prepared.variant
    model_id = prepared.model_id
    try:
        with session.begin_nested():
            # When a model-specific card's window_type changes (e.g. Antigravity
            # switching from "session" to "weekly" on cooldown), delete any stale rows
            # for the same (provider, account, variant, model) that carry a different
            # window_type. Because window_type is part of the unique key, an upsert
            # would otherwise create a second row alongside the old one indefinitely.
            #
            # Guard: only fire for model-specific cards (model_id != ""). Aggregate
            # cards (model_id == "") can legitimately have multiple window_types for
            # the same provider — e.g. Anthropic emits both a "session" card (five_hour)
            # and a "weekly" card (seven_day) at the aggregate level. Deleting across
            # window_types for those would silently destroy valid data.
            if model_id:
                # `col()` is needed here (vs the bare `Model.field == x` style
                # used elsewhere) because SQLModel's `delete().where()` stubs
                # don't track the column-comparison return type the way
                # `select().where()` does — mypy sees a `bool` otherwise.
                session.exec(
                    delete(LatestUsage).where(
                        col(LatestUsage.provider_id) == card.provider_id,
                        col(LatestUsage.account_id) == canonical_account_id,
                        col(LatestUsage.variant) == variant,
                        col(LatestUsage.model_id) == model_id,
                        col(LatestUsage.window_type) != card.window_type,
                    )
                )

            existing = session.exec(
                select(LatestUsage).where(
                    LatestUsage.provider_id == card.provider_id,
                    LatestUsage.account_id == canonical_account_id,
                    LatestUsage.window_type == card.window_type,
                    LatestUsage.variant == variant,
                    LatestUsage.model_id == model_id,
                )
            ).first()

            # Window-close detection: if reset_at has advanced, archive the
            # just-closed window before overwriting.
            if existing and card.reset_at:
                close_previous_window(
                    session,
                    existing=existing,
                    card=card,
                    canonical_account_id=canonical_account_id,
                )

            merge_latest_usage_row(session, existing=existing, prepared=prepared)
    except Exception as e:
        logger.warning(
            f"LatestUsage upsert failed for "
            f"{card.provider_id}/{canonical_account_id}/{card.window_type}: {e}"
        )
        return False
    return True


def write_contribution(session: Session, prepared: _PreparedLatestUsage) -> None:
    """Record this source's card and rebuild the merged slot."""
    if prepared.is_error:
        return
    # Patch target is app.services.accumulator._upsert_contribution. Import it
    # here so the lookup happens at call time, after a test replaces the name.
    from app.services.accumulator import _upsert_contribution
    from app.services.latest_usage_contributions import _rebuild_latest_usage_slot

    card = prepared.card
    canonical_account_id = prepared.canonical_account_id
    try:
        _upsert_contribution(
            session,
            source_id=prepared.contribution_source_id,
            card=card,
            account_id=canonical_account_id,
            card_json=prepared.incoming_partial,
        )
        _rebuild_latest_usage_slot(
            session,
            provider_id=card.provider_id,
            account_id=canonical_account_id,
            window_type=card.window_type,
            variant=prepared.variant,
            model_id=prepared.model_id,
        )
    except Exception as exc:
        logger.warning(
            "LatestUsage contribution write failed for %s/%s/%s: %s",
            card.provider_id,
            canonical_account_id,
            card.window_type,
            exc,
        )


def evict_stale_raw_account(session: Session, prepared: _PreparedLatestUsage) -> None:
    """Drop the pre-canonicalization row stored under the raw account id."""
    from app.models.db import LatestUsage

    card = prepared.card
    # Evict any pre-canonicalization row stored under the raw account_id
    # (typically "default") when resolve_account_id mapped it to a different
    # canonical identity (e.g. an email). Avoids duplicate fleet entries.
    raw_account_id = card.account_id or "default"
    canonical_account_id = prepared.canonical_account_id
    if raw_account_id == canonical_account_id:
        return
    try:
        with session.begin_nested():
            stale = session.exec(
                select(LatestUsage).where(
                    LatestUsage.provider_id == card.provider_id,
                    LatestUsage.account_id == raw_account_id,
                    LatestUsage.window_type == card.window_type,
                    LatestUsage.variant == prepared.variant,
                    LatestUsage.model_id == prepared.model_id,
                )
            ).first()
            if stale:
                session.delete(stale)
    except Exception as e:
        logger.warning(
            f"Stale row eviction failed for "
            f"{card.provider_id}/{raw_account_id}/{card.window_type}: {e}"
        )


def evict_superseded_error_rows(session: Session, prepared: _PreparedLatestUsage) -> None:
    """Drop error rows a healthy card makes redundant."""
    if prepared.is_error:
        return

    from app.models.db import LatestUsage
    from app.services.accumulator import _is_error_card

    card = prepared.card
    canonical_account_id = prepared.canonical_account_id
    variant = prepared.variant
    model_id = prepared.model_id
    # When any healthy card lands, evict stale orphaned error rows:
    # - error rows for this exact (provider_id, canonical_account_id) outside
    #   the slot just written (different window_type, variant, or model_id)
    # - default-account error rows in the same slot when this is a real account
    try:
        with session.begin_nested():
            same_account_errors = session.exec(
                select(LatestUsage).where(
                    LatestUsage.provider_id == card.provider_id,
                    LatestUsage.account_id == canonical_account_id,
                    or_(
                        LatestUsage.window_type != card.window_type,
                        LatestUsage.variant != variant,
                        LatestUsage.model_id != model_id,
                    ),
                )
            ).all()
            for row in same_account_errors:
                if _is_error_card(row.card_json):
                    session.delete(row)

            if canonical_account_id != "default":
                slot_default_errors = session.exec(
                    select(LatestUsage).where(
                        LatestUsage.provider_id == card.provider_id,
                        LatestUsage.account_id == "default",
                        LatestUsage.window_type == card.window_type,
                        LatestUsage.variant == variant,
                        LatestUsage.model_id == model_id,
                    )
                ).all()
                for row in slot_default_errors:
                    if _is_error_card(row.card_json):
                        session.delete(row)
    except Exception as e:
        logger.warning(
            "Orphan error row eviction failed for %s/%s: %s",
            card.provider_id,
            canonical_account_id,
            e,
        )


def evict_stale_aggregate_error(session: Session, prepared: _PreparedLatestUsage) -> None:
    """Drop a model-less error placeholder once a per-model card arrives."""
    if prepared.is_error or prepared.model_id == "":
        return

    from app.models.db import LatestUsage

    card = prepared.card
    canonical_account_id = prepared.canonical_account_id
    # Evict a stale aggregate error card (model_id="") when a fresh per-model
    # card arrives for the same (provider, account, window_type, variant).
    # Transient collection failures write an error placeholder with model_id=""
    # that would otherwise persist alongside healthy per-model cards indefinitely.
    try:
        with session.begin_nested():
            stale_error = session.exec(
                select(LatestUsage).where(
                    LatestUsage.provider_id == card.provider_id,
                    LatestUsage.account_id == canonical_account_id,
                    LatestUsage.window_type == card.window_type,
                    LatestUsage.variant == prepared.variant,
                    LatestUsage.model_id == "",
                )
            ).first()
            if stale_error:
                stale_json = json.loads(stale_error.card_json or "{}")
                if stale_json.get("data_source") == "error" or stale_json.get("remaining") == "ERR":
                    session.delete(stale_error)
    except Exception as e:
        logger.warning(
            f"Stale error card eviction failed for "
            f"{card.provider_id}/{canonical_account_id}/{card.window_type}: {e}"
        )


def append_quota_snapshot(session: Session, prepared: _PreparedLatestUsage) -> None:
    """Append a quota snapshot when the card carries a usable percentage."""
    from app.services.accumulator import record_quota_snapshot

    card = prepared.card
    # Append quota snapshot for % used history (only when quota data is present).
    # Derive pct_used from used_value when unit_type=percent (e.g. Claude web scraper)
    # or from used_value/limit_value ratio when both are present.
    effective_pct = card.pct_used
    if effective_pct is None and card.used_value is not None:
        if card.unit_type == "percent":
            effective_pct = card.used_value
        elif card.limit_value and card.limit_value > 0:
            effective_pct = (card.used_value / card.limit_value) * 100.0

    if effective_pct is not None:
        reset_at_dt = None
        if card.reset_at:
            try:
                reset_at_dt = parse_iso8601_utc(card.reset_at)
            except Exception:
                logger.debug("Failed to parse reset_at for snapshot", exc_info=True)
        record_quota_snapshot(
            session,
            provider_id=card.provider_id,
            account_id=prepared.canonical_account_id,
            window_type=card.window_type,
            variant=prepared.variant,
            model_id=prepared.model_id,
            pct_used=effective_pct,
            reset_at=reset_at_dt,
        )
