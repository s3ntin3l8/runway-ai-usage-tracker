# app/services/accumulator.py
import json
import logging
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, select

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
    data = json.loads(card_json or "{}")
    return (
        bool(data.get("error_type"))
        or data.get("data_source") == "error"
        or data.get("remaining") == "ERR"
    )


def upsert_latest_usage(  # noqa: PLR0915
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

    is_error = (
        bool(incoming_partial.get("error_type"))
        or incoming_partial.get("data_source") == "error"
        or incoming_partial.get("remaining") == "ERR"
    )

    # Suppress error cards when a healthy row already covers this account slot.
    # A failed poll cycle must not write a permanent orphan row that shows up
    # as an extra fleet-strip light next to the healthy row it can't evict.
    if is_error:
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
                return
            if canonical_account_id == "default":
                real_row = session.exec(
                    select(LatestUsage).where(
                        LatestUsage.provider_id == card.provider_id,
                        LatestUsage.account_id != "default",
                    )
                ).first()
                if real_row:
                    logger.debug(
                        "Skipping default-account error card for %s — real account row exists",
                        card.provider_id,
                    )
                    return
        except Exception as e:
            logger.debug("Error-suppression check failed for %s: %s", card.provider_id, e)

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

    # When a healthy real-account card lands, evict stale orphaned error rows:
    # - default-account error rows written by past failed poll cycles
    # - same-account error rows under a different variant (left by prior code)
    if not is_error and canonical_account_id != "default":
        try:
            with session.begin_nested():
                orphan_defaults = session.exec(
                    select(LatestUsage).where(
                        LatestUsage.provider_id == card.provider_id,
                        LatestUsage.account_id == "default",
                    )
                ).all()
                for row in orphan_defaults:
                    if _is_error_card(row.card_json):
                        session.delete(row)
                cross_variant_errors = session.exec(
                    select(LatestUsage).where(
                        LatestUsage.provider_id == card.provider_id,
                        LatestUsage.account_id == canonical_account_id,
                        LatestUsage.variant != variant,
                    )
                ).all()
                for row in cross_variant_errors:
                    if _is_error_card(row.card_json):
                        session.delete(row)
        except Exception as e:
            logger.warning(
                "Orphan error row eviction failed for %s/%s: %s",
                card.provider_id,
                canonical_account_id,
                e,
            )

    # Evict a stale aggregate error card (model_id="") when a fresh per-model
    # card arrives for the same (provider, account, window_type, variant).
    # Transient collection failures write an error placeholder with model_id=""
    # that would otherwise persist alongside healthy per-model cards indefinitely.
    if not is_error and model_id != "":
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
            variant=variant,
            model_id=model_id,
            pct_used=effective_pct,
            reset_at=reset_at_dt,
        )


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
                    reset_dt = datetime.fromisoformat(
                        raw_reset.replace("Z", "+00:00")
                        if isinstance(raw_reset, str)
                        else raw_reset.isoformat()
                    )
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
    Returns the number of rows deleted.
    """
    from app.models.db import LatestUsage

    deleted = 0
    try:
        all_rows = session.exec(select(LatestUsage)).all()
        # Index non-error rows by provider_id and (provider_id, account_id)
        healthy_accounts: set[tuple[str, str]] = set()
        providers_with_real_account: set[str] = set()
        for row in all_rows:
            if not _is_error_card(row.card_json):
                healthy_accounts.add((row.provider_id, row.account_id))
                if row.account_id != "default":
                    providers_with_real_account.add(row.provider_id)

        to_delete = []
        for row in all_rows:
            if not _is_error_card(row.card_json):
                continue
            pid, aid = row.provider_id, row.account_id
            if (pid, aid) in healthy_accounts:
                # A healthy row for the same account supersedes this error row
                to_delete.append(row)
            elif aid == "default" and pid in providers_with_real_account:
                # A real-account row for this provider supersedes the default-orphan
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
