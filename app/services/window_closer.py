"""Window-close aggregation.

Detects when a provider's authoritative reset_at has advanced and captures
the just-closed window's final totals into usage_windows.

Each window-close emits the cartesian product:
  - ('', '')          all-models / all-sidecars rollup
  - (model, '')       per-model rollups (one per distinct non-empty model_id)
  - ('', sidecar)     per-sidecar rollups (one per distinct sidecar_id)
  - (model, sidecar)  full breakdown cells

All inserts are idempotent via UNIQUE constraint + IntegrityError catch.

Session contract: caller owns commit. close_window never commits or rolls back
the outer session — it uses per-row SAVEPOINTs (begin_nested) to absorb
IntegrityError on duplicate inserts without disturbing the caller's transaction.
"""

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.date_utils import parse_iso8601_utc
from app.models.db import LatestUsage, UsageEvent, UsageWindow

logger = logging.getLogger(__name__)

WINDOW_DURATION: dict[str, timedelta] = {
    "session": timedelta(hours=5),
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}


def close_window(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str,
    window_start: datetime,
    window_end: datetime,
    limit_value: float | None = None,
    pct_used: float | None = None,
) -> int:
    """Aggregate usage_events in [window_start, window_end) and write usage_windows rows.

    Inserts rows for: ('', ''), ({model}, ''), ('', {sidecar}), ({model}, {sidecar}).
    Returns total rows inserted (idempotent via UNIQUE constraint, returns 0 on replay).

    `kind="error"` events are skipped — they represent provider failures, not
    billable usage. This matches event_ingestor, which also excludes them from
    period rollups, keeping window totals consistent with rollup sums.
    """
    # Query message events in the window range for this provider + account.
    # Error events are tracked in usage_events for diagnostics but never roll
    # up into quota windows.
    events = session.exec(
        select(UsageEvent).where(
            UsageEvent.provider_id == provider_id,
            UsageEvent.account_id == account_id,
            UsageEvent.ts >= window_start,
            UsageEvent.ts < window_end,
            UsageEvent.kind != "error",
        )
    ).all()

    if not events:
        return 0

    # Aggregate at the different grains
    # grain_key: (model_id, sidecar_id) -> {msgs, tokens_input, ...}
    agg: dict[tuple[str, str], dict] = {}

    def _ensure(key: tuple[str, str]) -> dict:
        if key not in agg:
            agg[key] = {
                "msgs": 0,
                "tokens_input": 0,
                "tokens_output": 0,
                "tokens_cache_read": 0,
                "tokens_cache_create": 0,
                "tokens_reasoning": 0,
                "cost_usd": 0.0,
            }
        return agg[key]

    for ev in events:
        model = ev.model_id or ""
        sidecar = ev.sidecar_id or ""

        grains: list[tuple[str, str]] = [
            ("", ""),
            (model, ""),
            ("", sidecar),
            (model, sidecar),
        ]
        # deduplicate (e.g. when model is "" the first two grains are identical)
        seen: dict[tuple[str, str], None] = {}
        for g in grains:
            seen[g] = None

        for key in seen:
            totals = _ensure(key)
            totals["msgs"] += 1
            totals["tokens_input"] += ev.tokens_input
            totals["tokens_output"] += ev.tokens_output
            totals["tokens_cache_read"] += ev.tokens_cache_read
            totals["tokens_cache_create"] += ev.tokens_cache_create
            totals["tokens_reasoning"] += ev.tokens_reasoning
            totals["cost_usd"] += ev.cost_usd

    # Write rows — one per grain, idempotent via SAVEPOINT + IntegrityError catch.
    # Caller owns commit; we must not commit or rollback the outer session.
    inserted = 0
    for (model_id, sidecar_id), totals in agg.items():
        row = UsageWindow(
            provider_id=provider_id,
            account_id=account_id,
            window_type=window_type,
            window_start=window_start,
            window_end=window_end,
            model_id=model_id,
            sidecar_id=sidecar_id,
            msgs=totals["msgs"],
            tokens_input=totals["tokens_input"],
            tokens_output=totals["tokens_output"],
            tokens_cache_read=totals["tokens_cache_read"],
            tokens_cache_create=totals["tokens_cache_create"],
            tokens_reasoning=totals["tokens_reasoning"],
            cost_usd=totals["cost_usd"],
            limit_value=limit_value,
            pct_used=pct_used,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
            inserted += 1
        except IntegrityError:
            # Duplicate — SAVEPOINT already rolled back; skip this grain
            continue

    return inserted


def _maybe_close_previous_window(
    session: Session,
    *,
    existing: LatestUsage | None,
    provider_id: str,
    account_id: str,
    window_type: str,
    new_reset_at: datetime,
) -> int:
    """Detect if the previous window has closed and archive it into usage_windows.

    Compares new_reset_at against the reset_at stored in existing.card_json.
    If new_reset_at is strictly later (and window_type is a known duration),
    calls close_window() to capture the just-closed window's final totals.

    Returns the number of usage_windows rows inserted (0 if no window closed).

    Window types not in WINDOW_DURATION (e.g. weekly_sonnet, weekly_design)
    are intentionally skipped — they are covered as per-model rows inside the
    standard weekly close, and we have no authoritative duration for them.
    """
    if existing is None or not existing.card_json:
        return 0
    if window_type not in WINDOW_DURATION:
        return 0

    try:
        existing_data = json.loads(existing.card_json)
        existing_reset_str = existing_data.get("reset_at")
        if not existing_reset_str:
            return 0

        existing_reset_dt = parse_iso8601_utc(existing_reset_str)

        if new_reset_at <= existing_reset_dt:
            return 0  # reset_at has not advanced — no window closed

        # Previous window closed at existing_reset_dt
        window_start = existing_reset_dt - WINDOW_DURATION[window_type]
        return close_window(
            session,
            provider_id=provider_id,
            account_id=account_id,
            window_type=window_type,
            window_start=window_start,
            window_end=existing_reset_dt,
            limit_value=existing_data.get("limit_value"),
            pct_used=existing_data.get("pct_used"),
        )
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.debug(f"Window-close check skipped for {provider_id}/{account_id}: {e}")
        return 0
