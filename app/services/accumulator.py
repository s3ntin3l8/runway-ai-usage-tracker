# app/services/accumulator.py
import json
import logging
from datetime import UTC, datetime

from sqlmodel import Session, select

logger = logging.getLogger(__name__)


def _join_distinct(
    a: str | None, b: str | None, *, drop: frozenset[str] = frozenset()
) -> str | None:
    if not drop:
        if a == b or b is None:
            return a
    parts = dict.fromkeys(p for s in (a, b) for p in (s or "").split(",") if p and p not in drop)
    return ",".join(parts) or None


# Quota fields that must stay unit-consistent — don't let a token-unit enrichment
# source overwrite percentage-unit quota data from the web collector, or vice versa.
_QUOTA_FIELDS = frozenset({"used_value", "limit_value", "pct_used", "unit_type", "currency"})


def record_quota_snapshot(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str,
    model_id: str,
    pct_used: float,
    reset_at=None,
) -> None:
    """Append a quota snapshot row. Silently ignores same-minute duplicates."""
    from app.models.db import QuotaSnapshot

    # Truncate to the minute so that a sidecar push followed immediately by a
    # server-side poller wake (both within the same minute) don't produce two
    # identical-looking rows in the history table.
    ts = datetime.now(UTC).replace(second=0, microsecond=0)
    try:
        with session.begin_nested():
            session.add(
                QuotaSnapshot(
                    provider_id=provider_id,
                    account_id=account_id,
                    window_type=window_type,
                    model_id=model_id,
                    ts=ts,
                    pct_used=pct_used,
                    reset_at=reset_at,
                )
            )
    except Exception:
        pass  # unique constraint violation = same-minute duplicate, safe to ignore


def merge_card_json(existing: str | None, incoming: dict) -> str:
    """Merge an incoming card payload into an existing row's JSON; pass partial dicts, not full model_dump()."""
    if not existing:
        return json.dumps(incoming)

    existing_dict = json.loads(existing)
    merged = {**existing_dict}

    existing_unit = existing_dict.get("unit_type")
    incoming_unit = incoming.get("unit_type")
    # When units conflict (e.g. local enrichment sends tokens into a percent-based
    # quota row) protect the quota fields so the existing quota data is preserved.
    unit_mismatch = bool(existing_unit and incoming_unit and existing_unit != incoming_unit)

    # Error cards represent transient poll failures — don't let them overwrite the
    # source provenance of a previously successful card. Any other fields (health,
    # detail, error_type) still merge so the UI surfaces the error state.
    is_error_card = incoming.get("data_source") == "error" or incoming.get("remaining") == "ERR"

    for key, value in incoming.items():
        if key == "by_model":
            # {} means "not populated by this source" — legitimate empty resets are unrepresentable
            if isinstance(value, dict) and value:
                merged[key] = value
        elif key in ("data_source", "input_source"):
            if is_error_card:
                pass  # preserve existing provenance; don't join "error"/"unknown" in
            else:
                # Drop stale "error" tokens accumulated from past failure cards.
                merged[key] = _join_distinct(
                    existing_dict.get(key), value, drop=frozenset({"error"})
                )
        elif value is not None:
            if unit_mismatch and key in _QUOTA_FIELDS:
                continue
            merged[key] = value

    return json.dumps(merged)


def upsert_latest_usage(
    session: Session,
    card_dict: dict,
    *,
    sidecar_id_override: str | None = None,
) -> None:
    """Upsert a card dict into LatestUsage, merging with any existing row.

    This is the canonical write path shared by the background poller and the
    /fleet/ingest endpoint. Both paths must stay in sync — add features here,
    not in callers.

    Includes:
    - resolve_account_id canonicalisation
    - window-close detection (_maybe_close_previous_window)
    - stale raw-account-id row eviction
    - begin_nested savepoint so a single bad card can't abort the caller's
      transaction

    Args:
        session:            Active SQLModel Session (caller owns commit).
        card_dict:          Raw dict (e.g. LimitCard.model_dump(exclude_none=True)).
        sidecar_id_override: Override for sidecar_id column; falls back to
                            card_dict["sidecar_id"] then "local".
    """
    from app.models.db import LatestUsage
    from app.models.schemas import LimitCard
    from app.services.account_identity import resolve_account_id
    from app.services.poller import _maybe_close_previous_window

    try:
        card = LimitCard(**card_dict)
    except Exception as e:
        logger.warning(f"upsert_latest_usage: invalid card shape — {e}")
        return

    if not card.provider_id or not card.account_id:
        return
    if card.data_source == "cache":
        return

    canonical_account_id = resolve_account_id(card.provider_id, card.account_id, card.account_label)
    sidecar_id = sidecar_id_override or card.sidecar_id or "local"
    variant = card.variant or "default"
    model_id = card.model_id or ""
    incoming_partial = card.model_dump(exclude_none=True)
    # Always embed the canonical account_id so the card_json grouping key
    # matches the column (fleet API groups by card_json, not by the column).
    incoming_partial["account_id"] = canonical_account_id

    try:
        with session.begin_nested():
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
                try:
                    new_reset_dt = datetime.fromisoformat(
                        card.reset_at.replace("Z", "+00:00")
                        if isinstance(card.reset_at, str)
                        else card.reset_at.isoformat()
                    )
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
                        f"Window-close detection skipped for "
                        f"{card.provider_id}/{canonical_account_id}: {exc}"
                    )

            if existing:
                existing.card_json = merge_card_json(existing.card_json, incoming_partial)
                existing.sidecar_id = sidecar_id
                existing.updated_at = datetime.now(UTC)
            else:
                session.add(
                    LatestUsage(
                        provider_id=card.provider_id,
                        account_id=canonical_account_id,
                        sidecar_id=sidecar_id,
                        window_type=card.window_type,
                        variant=variant,
                        model_id=model_id,
                        card_json=merge_card_json(None, incoming_partial),
                    )
                )
    except Exception as e:
        logger.warning(
            f"LatestUsage upsert failed for "
            f"{card.provider_id}/{canonical_account_id}/{card.window_type}: {e}"
        )
        return

    # Evict any pre-canonicalization row stored under the raw account_id
    # (typically "default") when resolve_account_id mapped it to a different
    # canonical identity (e.g. an email). Avoids duplicate fleet entries.
    raw_account_id = card.account_id or "default"
    if raw_account_id != canonical_account_id:
        try:
            with session.begin_nested():
                stale = session.exec(
                    select(LatestUsage).where(
                        LatestUsage.provider_id == card.provider_id,
                        LatestUsage.account_id == raw_account_id,
                        LatestUsage.window_type == card.window_type,
                        LatestUsage.variant == variant,
                        LatestUsage.model_id == model_id,
                    )
                ).first()
                if stale:
                    session.delete(stale)
        except Exception as e:
            logger.warning(
                f"Stale row eviction failed for "
                f"{card.provider_id}/{raw_account_id}/{card.window_type}: {e}"
            )

    # Evict a stale aggregate error card (model_id="") when a fresh per-model
    # card arrives for the same (provider, account, window_type, variant).
    # Transient collection failures write an error placeholder with model_id=""
    # that would otherwise persist alongside healthy per-model cards indefinitely.
    is_error_incoming = (
        incoming_partial.get("data_source") == "error"
        or incoming_partial.get("remaining") == "ERR"
    )
    if not is_error_incoming and model_id != "":
        try:
            with session.begin_nested():
                stale_error = session.exec(
                    select(LatestUsage).where(
                        LatestUsage.provider_id == card.provider_id,
                        LatestUsage.account_id == canonical_account_id,
                        LatestUsage.window_type == card.window_type,
                        LatestUsage.variant == variant,
                        LatestUsage.model_id == "",
                    )
                ).first()
                if stale_error:
                    stale_json = json.loads(stale_error.card_json or "{}")
                    if (
                        stale_json.get("data_source") == "error"
                        or stale_json.get("remaining") == "ERR"
                    ):
                        session.delete(stale_error)
        except Exception as e:
            logger.warning(
                f"Stale error card eviction failed for "
                f"{card.provider_id}/{canonical_account_id}/{card.window_type}: {e}"
            )

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
                reset_at_dt = datetime.fromisoformat(
                    card.reset_at.replace("Z", "+00:00")
                    if isinstance(card.reset_at, str)
                    else card.reset_at.isoformat()
                )
            except Exception:
                pass
        record_quota_snapshot(
            session,
            provider_id=card.provider_id,
            account_id=canonical_account_id,
            window_type=card.window_type,
            model_id=model_id,
            pct_used=effective_pct,
            reset_at=reset_at_dt,
        )
