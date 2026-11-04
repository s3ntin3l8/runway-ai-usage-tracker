# app/services/accumulator.py
import json
import logging
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, select

from app.core.date_utils import parse_iso8601_utc
from app.core.utils import is_error_card_dict
from app.services import accumulator_upsert
from app.services.latest_usage_contributions import (
    _upsert_contribution,
    backfill_latest_usage_contributions,
    mark_latest_usage_source_stale,
    prune_orphan_latest_usage_contributions,
    reconcile_latest_usage_snapshot,
)

# Re-exported so existing imports and patches of app.services.accumulator
# keep resolving the moved contribution helpers.
__all__ = [
    "_upsert_contribution",
    "backfill_latest_usage_contributions",
    "evict_orphan_error_rows",
    "mark_latest_usage_source_stale",
    "merge_card_json",
    "prune_orphan_latest_usage_contributions",
    "prune_stale_latest_usage",
    "reconcile_latest_usage_snapshot",
    "record_quota_snapshot",
    "upsert_latest_usage",
]

logger = logging.getLogger(__name__)

# Keep ORM model imports inside persistence functions so importing the
# accumulator during application startup does not eagerly register DB models.


def _source_id(provider_id: str, sidecar_id: str) -> str:
    return (
        f"sidecar:{sidecar_id}:{provider_id}" if sidecar_id != "local" else f"server:{provider_id}"
    )


def _parse_source_id(source_id: str) -> tuple[str, str, str]:
    """Return (producer kind, producer scope, provider id)."""
    parts = source_id.split(":", 2)
    if len(parts) == 2 and parts[0] == "server":
        return "server", "local", parts[1]
    if len(parts) == 3 and parts[0] == "sidecar":
        return "sidecar", parts[1], parts[2]
    raise ValueError(f"Invalid LatestUsage source id: {source_id!r}")


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
    variant: str,
    model_id: str,
    pct_used: float,
    reset_at=None,
) -> None:
    """Append a quota snapshot row. Silently ignores same-minute duplicates."""
    from app.models.db import QuotaSnapshot

    # Normalize to match the column default ("") so the forecast read side
    # (which uses `card.variant or ""`) can find these rows.
    # LatestUsage writes "default" as a sentinel, but QuotaSnapshot doesn't need it.
    variant = "" if variant in ("default", None) else variant

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
                    variant=variant,
                    model_id=model_id,
                    ts=ts,
                    pct_used=pct_used,
                    reset_at=reset_at,
                )
            )
    except IntegrityError:
        # Unique constraint = same-minute duplicate, safe to ignore. Any
        # other DB error (connection drop, schema mismatch) should bubble
        # up so the caller can log it loudly instead of hiding silently.
        pass


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

    # Recovery clear: a fresh non-error card carrying quota data wipes residual error
    # stamps left over from a prior failed poll. Collectors use exclude_none=True so
    # error_type=None is never sent — without this, a once-set error_type sticks
    # forever and the frontend hero filter keeps excluding the (now-healthy) row.
    incoming_has_fresh_quota = any(
        incoming.get(k) is not None for k in ("used_value", "limit_value", "pct_used")
    )
    if not is_error_card and incoming_has_fresh_quota:
        merged.pop("error_type", None)
        # Sticky stale/collection_failing: full model_dump(exclude_none=True)
        # always emits stale=False + collection_failing=False (bool defaults are
        # not None), so the loop below already clears them on the real upsert
        # path. The pops cover partial dicts (merge_card_json's documented API)
        # that carry fresh quota but omit the keys entirely — without them,
        # residual stale=True / collection_failing=True would stick.
        merged.pop("stale", None)
        merged.pop("collection_failing", None)
        # Sticky billing evidence: a card the account has since published
        # carries no billing_evidence key at all, so the "placeholder" a
        # previous poll wrote would live forever and keep the account
        # unpriceable long after it started publishing its own windows.
        merged.pop("billing_evidence", None)
        if merged.get("data_source") == "error":
            merged.pop("data_source", None)
        if merged.get("remaining") == "ERR":
            merged.pop("remaining", None)

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


def _is_error_card(card_json: str | None) -> bool:
    try:
        data = json.loads(card_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return is_error_card_dict(data)


def upsert_latest_usage(
    session: Session,
    card_dict: dict,
    *,
    sidecar_id_override: str | None = None,
    source_id: str | None = None,
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
    card = accumulator_upsert.validate_card(card_dict)
    if card is None:
        return
    prepared = accumulator_upsert.canonicalize_account(
        session,
        card,
        sidecar_id_override=sidecar_id_override,
        source_id=source_id,
    )
    if accumulator_upsert.error_card_is_suppressed(session, prepared):
        return
    if not accumulator_upsert.savepoint_upsert(session, prepared):
        return
    accumulator_upsert.write_contribution(session, prepared)
    accumulator_upsert.evict_stale_raw_account(session, prepared)
    accumulator_upsert.evict_superseded_error_rows(session, prepared)
    accumulator_upsert.evict_stale_aggregate_error(session, prepared)
    accumulator_upsert.append_quota_snapshot(session, prepared)


def prune_stale_latest_usage(
    session: Session,
    batch_keys: dict[tuple[str, str], set[tuple[str, str, str]]],
) -> int:
    """Delete latest_usage rows that the sidecar no longer reports AND whose
    window has already closed.

    Triggered at the end of a sidecar push. For each (provider_id, account_id)
    pair covered by the payload, any row whose (window_type, variant, model_id)
    is not in the just-pushed set AND whose card_json carries a reset_at in the
    past is treated as a ghost — the source dropped the model from its model
    list (Antigravity rotates these regularly) and the prior window has expired.
    Returns the number of rows deleted.

    Scoped tightly to pairs in the batch so a partial collector cycle (one
    provider pushing a single card) never prunes another provider's healthy
    rows.
    """
    from app.models.db import LatestUsage

    if not batch_keys:
        return 0

    now = datetime.now(UTC)
    deleted = 0
    # First pass: walk each (pid, aid) and collect the row IDs to prune.
    # Doing the JSON parse / reset_at check in Python is unavoidable (the
    # reset_at lives inside the JSON blob), but the actual DELETE happens
    # in a single bulk statement per pair instead of N round trips.
    for (pid, aid), keys in batch_keys.items():
        try:
            rows = session.exec(
                select(LatestUsage).where(
                    LatestUsage.provider_id == pid,
                    LatestUsage.account_id == aid,
                )
            ).all()
            stale_ids: list[int] = []
            for row in rows:
                row_key = (row.window_type, row.variant, row.model_id)
                if row_key in keys:
                    continue
                try:
                    data = json.loads(row.card_json or "{}")
                except (ValueError, TypeError):
                    continue
                raw_reset = data.get("reset_at")
                if not raw_reset:
                    continue
                try:
                    reset_dt = parse_iso8601_utc(raw_reset)
                except (ValueError, AttributeError, TypeError):
                    continue
                if reset_dt >= now:
                    continue
                logger.info(
                    "Pruning ghost latest_usage row %s/%s %s/%s/%s (reset_at=%s in past)",
                    pid,
                    aid,
                    row.window_type,
                    row.variant,
                    row.model_id,
                    raw_reset,
                )
                if row.id is not None:
                    stale_ids.append(row.id)

            if stale_ids:
                session.exec(delete(LatestUsage).where(col(LatestUsage.id).in_(stale_ids)))
                deleted += len(stale_ids)
        except Exception as e:
            logger.warning("Stale-row prune failed for %s/%s: %s", pid, aid, e)
    return deleted


def evict_orphan_error_rows(session: Session) -> int:
    """One-shot cleanup: remove error rows that are superseded by healthy rows.

    Run once at startup so existing stale rows from before the suppression
    logic was added are removed without waiting for the next collect cycle.

    Multi-account hardened: a default-tagged error row is only evicted when
    a non-default healthy row already covers the SAME `(provider_id,
    window_type, variant, model_id)` slot. Pre-fix logic checked
    provider-level only and would silently kill a legitimate default-tagged
    error card at a slot no real-account row covers.
    Returns the number of rows deleted.
    """
    from app.models.db import LatestUsage

    deleted = 0
    try:
        all_rows = session.exec(select(LatestUsage)).all()
        # Index non-error rows by (provider_id, account_id) for same-account
        # eviction, and by full slot tuple for the default-orphan case. Only
        # default-tagged error rows whose slot already has a real-account
        # counterpart are eligible for default-orphan eviction.
        healthy_accounts: set[tuple[str, str]] = set()
        slot_has_real_account: set[tuple[str, str, str, str]] = set()
        for row in all_rows:
            if not _is_error_card(row.card_json):
                healthy_accounts.add((row.provider_id, row.account_id))
                if row.account_id != "default":
                    slot_has_real_account.add(
                        (row.provider_id, row.window_type, row.variant, row.model_id)
                    )

        to_delete = []
        for row in all_rows:
            if not _is_error_card(row.card_json):
                continue
            pid, aid = row.provider_id, row.account_id
            slot = (pid, row.window_type, row.variant, row.model_id)
            if (pid, aid) in healthy_accounts:
                # A healthy row for the same account supersedes this error row
                to_delete.append(row)
            elif aid == "default" and slot in slot_has_real_account:
                # A real-account row covers this slot — the default-tagged
                # error card is redundant and safe to drop.
                to_delete.append(row)

        for row in to_delete:
            try:
                with session.begin_nested():
                    session.delete(row)
                deleted += 1
            except Exception as e:
                logger.warning("evict_orphan_error_rows: delete failed: %s", e)

        if deleted:
            logger.info("evict_orphan_error_rows: removed %d stale error row(s)", deleted)
    except Exception as e:
        logger.warning("evict_orphan_error_rows failed: %s", e)
    return deleted
