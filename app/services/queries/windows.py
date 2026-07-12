"""Auto-extracted from app.services.event_query during the monolith split.
See app/services/queries/__init__.py for the public surface.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from app.models._datetime import iso_utc
from app.models.db import UsageEvent, UsageWindow
from app.services.window_closer import WINDOW_DURATION

# ---------------------------------------------------------------------------
# 7.2  query_window_history
# ---------------------------------------------------------------------------


def query_window_history(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Return the N most recent closed windows, each with totals/by_model/by_sidecar.

    Row classification (per spec §6.2):
    - model_id='' AND sidecar_id=''  → totals + limit_value / pct_used
    - model_id!='' AND sidecar_id='' → by_model[]
    - model_id='' AND sidecar_id!='' → by_sidecar[]
    - both non-empty                 → dropped (cross-product rows)
    """
    stmt = (
        select(UsageWindow)
        .where(
            UsageWindow.provider_id == provider_id,
            UsageWindow.account_id == account_id,
            UsageWindow.window_type == window_type,
        )
        .order_by(UsageWindow.window_end.desc())  # type: ignore[attr-defined]
    )

    # Identify the N most-recent window_end values (the "top N windows")
    all_rows = list(session.exec(stmt).all())
    if not all_rows:
        return []

    # Collect unique window_ends in desc order, take the top N
    seen_ends: list[datetime] = []
    for row in all_rows:
        if row.window_end not in seen_ends:
            seen_ends.append(row.window_end)
        if len(seen_ends) == limit:
            break

    # Filter rows to only those in the selected window_ends
    allowed_ends = set(seen_ends)
    rows = [r for r in all_rows if r.window_end in allowed_ends]

    # Group by (window_start, window_end)
    window_map: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = (r.window_start, r.window_end)
        if key not in window_map:
            window_map[key] = {
                "window_start": iso_utc(r.window_start),
                "window_end": iso_utc(r.window_end),
                "totals": None,
                "by_model": [],
                "by_sidecar": [],
                "limit_value": None,
                "pct_used": None,
            }

        if r.model_id == "" and r.sidecar_id == "":
            # all-up totals row
            window_map[key]["totals"] = _window_row_totals(r)
            window_map[key]["limit_value"] = r.limit_value
            window_map[key]["pct_used"] = r.pct_used

        elif r.model_id != "" and r.sidecar_id == "":
            # per-model row
            window_map[key]["by_model"].append({"model_id": r.model_id, **_window_row_totals(r)})

        elif r.model_id == "" and r.sidecar_id != "":
            # per-sidecar row
            window_map[key]["by_sidecar"].append(
                {"sidecar_id": r.sidecar_id, **_window_row_totals(r)}
            )
        # else: cross-product row (both non-empty) — drop per spec

    # Sort by window_end desc (most recent first)
    result = sorted(window_map.values(), key=lambda w: w["window_end"], reverse=True)

    # Fill empty totals with zeroed dict if no totals row was present
    for w in result:
        if w["totals"] is None:
            w["totals"] = {
                "msgs": 0,
                "tokens_input": 0,
                "tokens_output": 0,
                "tokens_cache_read": 0,
                "tokens_cache_create": 0,
                "tokens_reasoning": 0,
                "cost_usd": 0.0,
            }

    return result


def _window_row_totals(row: UsageWindow) -> dict[str, Any]:
    return {
        "msgs": row.msgs,
        "tokens_input": row.tokens_input,
        "tokens_output": row.tokens_output,
        "tokens_cache_read": row.tokens_cache_read,
        "tokens_cache_create": row.tokens_cache_create,
        "tokens_reasoning": row.tokens_reasoning,
        "cost_usd": row.cost_usd,
    }


# ---------------------------------------------------------------------------
# 15.1  query_window_aggregation
# ---------------------------------------------------------------------------


def query_window_aggregation(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str,
    reset_at: datetime,
) -> dict:
    """Aggregate usage_events for [reset_at - WINDOW_DURATION[window_type], reset_at)
    into token_usage + by_model + by_sidecar dicts."""
    duration = WINDOW_DURATION[window_type]
    window_start = reset_at - duration

    # One pass: per (model_id, sidecar_id) sums
    rows = session.exec(
        select(  # type: ignore[call-overload]
            UsageEvent.model_id,
            UsageEvent.sidecar_id,
            func.count(UsageEvent.id),  # type: ignore[arg-type]
            func.sum(UsageEvent.tokens_input),
            func.sum(UsageEvent.tokens_output),
            func.sum(UsageEvent.tokens_cache_read),
            func.sum(UsageEvent.tokens_cache_create),
            func.sum(UsageEvent.tokens_reasoning),
            func.sum(UsageEvent.cost_usd),
            func.sum(UsageEvent.cost_cache_read),
            func.sum(UsageEvent.cost_cache_create),
        )
        .where(
            UsageEvent.provider_id == provider_id,
            UsageEvent.account_id == account_id,
            UsageEvent.kind == "message",
            UsageEvent.ts >= window_start,
            UsageEvent.ts < reset_at,
        )
        .group_by(UsageEvent.model_id, UsageEvent.sidecar_id)
    ).all()

    # Roll up: total + by_model + by_sidecar
    total: dict[str, Any] = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_create": 0,
        "reasoning": 0,
        "msgs": 0,
        "cost": 0.0,
    }
    by_model: dict[str, dict] = {}
    by_sidecar: dict[str, dict] = {}

    for mid, sid, msgs, ti, to, tcr, tcc, tr, cost, ccr, ccc in rows:
        ti = ti or 0
        to = to or 0
        tcr = tcr or 0
        tcc = tcc or 0
        tr = tr or 0
        cost = cost or 0.0
        cost_cache = (ccr or 0.0) + (ccc or 0.0)  # cache portion of cost
        total["input"] += ti
        total["output"] += to
        total["cache_read"] += tcr
        total["cache_create"] += tcc
        total["reasoning"] += tr
        total["msgs"] += msgs
        total["cost"] += cost
        if mid:
            m = by_model.setdefault(
                mid,
                {
                    "tokens_input": 0,
                    "tokens_output": 0,
                    "tokens_cache_read": 0,
                    "tokens_cache_create": 0,
                    "tokens_reasoning": 0,
                    "msgs": 0,
                    "cost_usd": 0.0,
                    "cost_cache": 0.0,
                },
            )
            m["tokens_input"] += ti
            m["tokens_output"] += to
            m["tokens_cache_read"] += tcr
            m["tokens_cache_create"] += tcc
            m["tokens_reasoning"] += tr
            m["msgs"] += msgs
            m["cost_usd"] += cost
            m["cost_cache"] += cost_cache
        if sid:
            s = by_sidecar.setdefault(
                sid,
                {
                    "tokens_input": 0,
                    "tokens_output": 0,
                    "tokens_cache_read": 0,
                    "tokens_cache_create": 0,
                    "tokens_reasoning": 0,
                    "msgs": 0,
                    "cost_usd": 0.0,
                    "cost_cache": 0.0,
                },
            )
            s["tokens_input"] += ti
            s["tokens_output"] += to
            s["tokens_cache_read"] += tcr
            s["tokens_cache_create"] += tcc
            s["tokens_reasoning"] += tr
            s["msgs"] += msgs
            s["cost_usd"] += cost
            s["cost_cache"] += cost_cache

    token_usage = {
        "input": total["input"],
        "output": total["output"],
        "cache_read": total["cache_read"],
        "cache_create": total["cache_create"],
        "reasoning": total["reasoning"],
        "total": (
            total["input"]
            + total["output"]
            + total["cache_read"]
            + total["cache_create"]
            + total["reasoning"]
        ),
    }
    return {
        "window_type": window_type,
        "window_start": window_start.isoformat(),
        "window_end": reset_at.isoformat(),
        "token_usage": token_usage,
        "cost_usd": total["cost"],
        "by_model": by_model,
        "by_sidecar": by_sidecar,
    }
