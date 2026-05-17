"""Auto-extracted from app.services.event_query during the monolith split.
See app/services/queries/__init__.py for the public surface.
"""

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.models._datetime import iso_utc
from app.models.db import UsageEvent, UsagePeriodRollup, UsageWindow
from app.services.queries._shared import _parse_period_key
from app.services.window_closer import WINDOW_DURATION


def query_windows(
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = 30.0,
    window_type: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict:
    """Return paginated quota windows, newest first.

    Closed windows come from usage_windows (final pct_used stored).
    Open windows are synthesised from latest_usage cards (model_id='').
    """
    import json as _json
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from app.models.db import LatestUsage

    since = datetime.now(UTC) - timedelta(days=days)

    stmt = select(UsageWindow).where(
        UsageWindow.window_end >= since,
        UsageWindow.model_id == "",
        UsageWindow.sidecar_id == "",
    )
    if provider_id:
        stmt = stmt.where(UsageWindow.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsageWindow.account_id == account_id)
    if window_type and window_type != "all":
        stmt = stmt.where(UsageWindow.window_type == window_type)

    def _derive_pct(card_dict: dict) -> float | None:
        """Derive a pct_used value from a card dict (used_value + unit_type fallback)."""
        pct = card_dict.get("pct_used")
        if pct is not None:
            return pct
        used = card_dict.get("used_value")
        if used is None:
            return None
        if card_dict.get("unit_type") == "percent":
            return float(used)
        lim = card_dict.get("limit_value")
        if lim and lim > 0:
            return (used / lim) * 100.0
        return None

    # Deduplicate closed windows: usage_windows has microsecond-distinct rows for the
    # same logical window (each poll slightly adjusts window_end). Keep the row with the
    # highest token count per (provider, account, window_type, date(window_end)).
    seen_closed: dict[tuple, int] = {}  # key → best tokens_total
    raw_closed = session.exec(stmt).all()
    dedup_closed: dict[tuple, UsageWindow] = {}
    for w in raw_closed:
        day_key = (
            w.provider_id,
            w.account_id,
            w.window_type,
            str(w.window_end)[:10],  # YYYY-MM-DD, ignores microsecond drift
        )
        total_toks = (
            w.tokens_input
            + w.tokens_output
            + w.tokens_cache_read
            + w.tokens_cache_create
            + w.tokens_reasoning
        )
        if day_key not in seen_closed or total_toks > seen_closed[day_key]:
            seen_closed[day_key] = total_toks
            dedup_closed[day_key] = w

    rows: list[dict] = []
    for w in dedup_closed.values():
        total_toks = seen_closed[
            (w.provider_id, w.account_id, w.window_type, str(w.window_end)[:10])
        ]
        rows.append(
            {
                "provider_id": w.provider_id,
                "account_id": w.account_id,
                "account_label": w.account_id,
                "service_name": w.provider_id.capitalize(),
                "window_type": w.window_type,
                "window_start": w.window_start.isoformat() if w.window_start else None,
                "window_end": w.window_end.isoformat() if w.window_end else None,
                "is_open": False,
                "pct_used": w.pct_used,
                "limit_value": w.limit_value,
                "unit_type": "tokens",
                "tokens_total": total_toks,
                "cost_usd": w.cost_usd,
                "msgs": w.msgs,
                "top_model": None,
            }
        )

    # Track which (provider, account, window_type) combos have an open window so we
    # can suppress the corresponding closed window (avoids duplicates for today).
    open_keys: set[tuple] = set()

    lu_stmt = select(LatestUsage).where(LatestUsage.model_id == "")
    if provider_id:
        lu_stmt = lu_stmt.where(LatestUsage.provider_id == provider_id)
    if account_id:
        lu_stmt = lu_stmt.where(LatestUsage.account_id == account_id)

    for lu in session.exec(lu_stmt).all():
        try:
            card = _json.loads(lu.card_json)
        except Exception:
            continue
        wt = lu.window_type
        if window_type and window_type not in {"all", wt}:
            continue
        reset_at = card.get("reset_at")

        # Apply the days filter to open windows: skip if reset_at is older than `since`.
        # Windows with no reset_at are always current (e.g. session-scoped).
        if reset_at:
            try:
                reset_dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                if reset_dt < since:
                    continue
            except Exception:
                pass

        token_usage = card.get("token_usage") or {}
        base_name = card.get("service_name", lu.provider_id.capitalize())
        variant = lu.variant if lu.variant and lu.variant != "default" else None
        service_name = f"{base_name} · {variant}" if variant else base_name
        open_keys.add((lu.provider_id, lu.account_id, wt))
        rows.append(
            {
                "provider_id": lu.provider_id,
                "account_id": lu.account_id,
                "account_label": card.get("account_label", lu.account_id),
                "service_name": service_name,
                "window_type": wt,
                "window_start": None,
                "window_end": reset_at,
                "is_open": True,
                "pct_used": _derive_pct(card),
                "limit_value": card.get("limit_value"),
                "unit_type": card.get("unit_type", "tokens"),
                "tokens_total": token_usage.get("total"),
                "cost_usd": card.get("cost_usd"),
                "msgs": card.get("msgs"),
                "top_model": None,
            }
        )

    # Drop closed windows that are superseded by an open window for the same
    # (provider, account, window_type) — they represent the same current window.
    rows = [
        r
        for r in rows
        if r["is_open"] or (r["provider_id"], r["account_id"], r["window_type"]) not in open_keys
    ]

    rows.sort(key=lambda r: r.get("window_end") or "9999", reverse=True)
    total = len(rows)
    offset = (page - 1) * limit
    return {"windows": rows[offset : offset + limit], "total": total, "page": page}


_PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Claude",
    "gemini": "Gemini",
    "openai": "OpenAI",
    "ollama": "Ollama",
    "openrouter": "OpenRouter",
    "kimi": "Kimi",
    "opencode": "OpenCode",
}


def query_snapshots(
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
    window_type: str | None = None,
    days: float = 7.0,
    page: int = 1,
    limit: int = 100,
) -> dict:
    """Flat paginated list of quota_snapshots, newest first, with per-series delta."""
    from app.models.db import QuotaSnapshot

    since = datetime.now(UTC) - timedelta(days=days)

    stmt = select(QuotaSnapshot).where(QuotaSnapshot.ts >= since)
    if provider_id:
        stmt = stmt.where(QuotaSnapshot.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(QuotaSnapshot.account_id == account_id)
    if window_type and window_type != "all":
        stmt = stmt.where(QuotaSnapshot.window_type == window_type)

    # Ascending for delta computation; reversed at output time
    stmt = stmt.order_by(QuotaSnapshot.ts.asc())  # type: ignore[attr-defined]
    all_snaps = list(session.exec(stmt).all())

    # Build window-level token/cost lookup keyed by (provider, account, window_type, minute_bucket)
    # reset_at and window_end timestamps differ by up to ~1s due to insertion jitter, so we
    # truncate to the minute for matching rather than relying on exact equality.

    def _min_bucket(dt: datetime) -> datetime:
        return dt.replace(second=0, microsecond=0)

    # Use naive UTC — QuotaSnapshot.reset_at is stored without tzinfo
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    window_stats: dict[tuple, dict] = {}

    # Closed windows: reset_at <= now → find matching usage_windows by minute-bucketed window_end
    past_resets = {s.reset_at for s in all_snaps if s.reset_at and s.reset_at <= now_naive}
    if past_resets:
        min_t = min(past_resets) - timedelta(minutes=2)
        max_t = max(past_resets) + timedelta(minutes=2)
        for w in session.exec(
            select(UsageWindow).where(
                UsageWindow.window_end >= min_t,
                UsageWindow.window_end <= max_t,
                UsageWindow.model_id == "",
                UsageWindow.sidecar_id == "",
            )
        ).all():
            tokens = (
                (w.tokens_input or 0)
                + (w.tokens_output or 0)
                + (w.tokens_cache_read or 0)
                + (w.tokens_cache_create or 0)
                + (w.tokens_reasoning or 0)
            )
            key = (w.provider_id, w.account_id, w.window_type, _min_bucket(w.window_end))
            if key not in window_stats:
                window_stats[key] = {
                    "tokens_total": tokens or None,
                    "cost_usd": w.cost_usd or None,
                }

    # Open windows: reset_at > now → sum usage_events in [window_start, now]
    # LatestUsage cards for quota-only providers (e.g. Claude) carry no token/cost data,
    # so we compute running totals directly from events.
    live_series = {
        (s.provider_id, s.account_id, s.window_type, _min_bucket(s.reset_at))
        for s in all_snaps
        if s.reset_at and s.reset_at > now_naive and s.window_type in WINDOW_DURATION
    }
    for pid, aid, wt, min_reset in live_series:
        actual_reset = next(
            (
                s.reset_at
                for s in all_snaps
                if s.provider_id == pid
                and s.account_id == aid
                and s.window_type == wt
                and s.reset_at
                and s.reset_at > now_naive
            ),
            None,
        )
        if not actual_reset:
            continue
        window_start = actual_reset - WINDOW_DURATION[wt]
        events = session.exec(
            select(UsageEvent).where(
                UsageEvent.provider_id == pid,
                UsageEvent.account_id == aid,
                UsageEvent.ts >= window_start,
                UsageEvent.ts <= now_naive,
            )
        ).all()
        tokens = sum(
            (e.tokens_input or 0)
            + (e.tokens_output or 0)
            + (e.tokens_cache_read or 0)
            + (e.tokens_cache_create or 0)
            + (e.tokens_reasoning or 0)
            for e in events
        )
        cost = sum(e.cost_usd or 0.0 for e in events)
        window_stats[(pid, aid, wt, min_reset)] = {
            "tokens_total": tokens or None,
            "cost_usd": cost or None,
        }

    # Group into per-series lists and compute deltas
    series: dict[tuple, list] = {}
    for s in all_snaps:
        series_key = (s.provider_id, s.account_id, s.window_type, s.model_id)
        series.setdefault(series_key, []).append(s)

    rows: list[dict] = []
    for (pid, aid, wt, mid), snaps in series.items():
        service = _PROVIDER_LABELS.get(pid, pid.capitalize())
        model_label = mid.capitalize() if mid else "-"
        for i, s in enumerate(snaps):
            prev_pct = snaps[i - 1].pct_used if i > 0 else None
            delta = (
                round(s.pct_used - prev_pct, 2)
                if (prev_pct is not None and s.pct_used is not None)
                else None
            )
            ts_iso = s.ts.isoformat() if s.ts.tzinfo else s.ts.isoformat() + "+00:00"
            reset_iso = (
                (s.reset_at.isoformat() if s.reset_at.tzinfo else s.reset_at.isoformat() + "+00:00")
                if s.reset_at
                else None
            )
            stats = (
                window_stats.get((pid, aid, wt, _min_bucket(s.reset_at)), {}) if s.reset_at else {}
            )
            rows.append(
                {
                    "provider_id": pid,
                    "account_id": aid,
                    "service_name": service,
                    "window_type": wt,
                    "model_id": mid,
                    "model_label": model_label,
                    "ts": ts_iso,
                    "pct_used": s.pct_used,
                    "delta": delta,
                    "reset_at": reset_iso,
                    "tokens_total": stats.get("tokens_total"),
                    "cost_usd": stats.get("cost_usd"),
                }
            )

    rows.sort(key=lambda r: r["ts"], reverse=True)

    total = len(rows)
    offset = (page - 1) * limit
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "rows": rows[offset : offset + limit],
    }


def query_chart(  # noqa: PLR0915 — known-debt: multi-metric chart aggregator, splits poorly
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = 30.0,
    metric: str = "percent",
) -> dict:
    """Return chart data.

    metric=percent  → fill curves from quota_snapshots.
    metric=tokens   → daily bars from usage_period_rollup.
    metric=cost     → daily bars (value=cost_usd) from usage_period_rollup.
    """
    import json as _json
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from app.models.db import QuotaSnapshot

    since = datetime.now(UTC) - timedelta(days=days)

    if metric == "percent":
        stmt = select(QuotaSnapshot).where(
            QuotaSnapshot.ts >= since,
            QuotaSnapshot.pct_used.isnot(None),  # type: ignore[union-attr]
        )
        if provider_id:
            stmt = stmt.where(QuotaSnapshot.provider_id == provider_id)
        if account_id:
            stmt = stmt.where(QuotaSnapshot.account_id == account_id)

        snaps = session.exec(stmt.order_by(QuotaSnapshot.ts)).all()  # type: ignore[arg-type]

        series_map: dict[str, dict] = {}
        for s in snaps:
            use_model = s.model_id or ""
            key = f"{s.provider_id}::{s.window_type}::{use_model}"
            if key not in series_map:
                label = f"{s.provider_id.capitalize()} · {s.window_type.capitalize()}"
                if use_model:
                    label += f" · {use_model}"
                series_map[key] = {
                    "key": key,
                    "provider_id": s.provider_id,
                    "window_type": s.window_type,
                    "model_id": use_model,
                    "label": label,
                    "color_hint": s.provider_id,
                    "points": [],
                }
            ts_iso = s.ts.isoformat() if s.ts.tzinfo else s.ts.isoformat() + "+00:00"
            series_map[key]["points"].append({"ts": ts_iso, "pct_used": s.pct_used})

        # Seed any provider/window_type that has current pct_used data in latest_usage
        # but no snapshots yet (e.g. first run after schema migration).
        from app.models.db import LatestUsage

        lu_all = session.exec(select(LatestUsage).where(LatestUsage.model_id == "")).all()
        now_iso = datetime.now(UTC).isoformat()
        for lu in lu_all:
            if provider_id and lu.provider_id != provider_id:
                continue
            if account_id and lu.account_id != account_id:
                continue
            try:
                card = _json.loads(lu.card_json)
            except Exception:
                continue
            # Derive pct_used (same logic as accumulator)
            pct: float | None = card.get("pct_used")
            if pct is None:
                used = card.get("used_value")
                if used is not None:
                    if card.get("unit_type") == "percent":
                        pct = float(used)
                    else:
                        lim = card.get("limit_value")
                        if lim and lim > 0:
                            pct = (used / lim) * 100.0
            if pct is None:
                continue
            wt = lu.window_type
            use_model = lu.model_id or ""
            key = f"{lu.provider_id}::{wt}::{use_model}"
            if key not in series_map:
                label = f"{lu.provider_id.capitalize()} · {wt.capitalize()}"
                series_map[key] = {
                    "key": key,
                    "provider_id": lu.provider_id,
                    "window_type": wt,
                    "model_id": use_model,
                    "label": label,
                    "color_hint": lu.provider_id,
                    "points": [],
                }
            # Only add seed point if this series has no snapshot yet (avoid duplicating last value)
            if not series_map[key]["points"]:
                series_map[key]["points"].append({"ts": now_iso, "pct_used": pct})

        return {"series": list(series_map.values())}

    # tokens or cost — hourly bars for days<=7, daily bars otherwise
    period_type = "hour" if days <= 7 else "day"
    since_key = (
        since.strftime("%Y-%m-%dT%H") if period_type == "hour" else since.strftime("%Y-%m-%d")
    )
    bar_stmt = select(UsagePeriodRollup).where(
        UsagePeriodRollup.period_type == period_type,
        UsagePeriodRollup.period_key >= since_key,
        UsagePeriodRollup.sidecar_id == "",
    )
    if provider_id:
        bar_stmt = bar_stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        bar_stmt = bar_stmt.where(UsagePeriodRollup.account_id == account_id)

    all_bar_rows = list(session.exec(bar_stmt.order_by(UsagePeriodRollup.period_key)).all())
    # Providers that have per-model rows for a given period — used to skip their aggregate row
    has_per_model: set[tuple[str, str]] = {
        (r.provider_id, r.period_key) for r in all_bar_rows if r.model_id != ""
    }

    bars_map: dict[str, list] = {}
    for r in all_bar_rows:
        if r.model_id == "" and (r.provider_id, r.period_key) in has_per_model:
            continue

        use_model = r.model_id
        key = r.period_key
        if key not in bars_map:
            bars_map[key] = []
        value = (
            r.cost_usd
            if metric == "cost"
            else r.tokens_input
            + r.tokens_output
            + r.tokens_cache_read
            + r.tokens_cache_create
            + r.tokens_reasoning
        )
        label = r.provider_id.capitalize()
        if use_model:
            label += f" · {use_model}"
        bars_map[key].append(
            {"provider_id": r.provider_id, "model_id": use_model, "label": label, "value": value}
        )

    bars = []
    for key in sorted(bars_map.keys()):
        ts = _parse_period_key(key, period_type)
        bars.append(
            {
                "date": ts.strftime("%Y-%m-%d") if ts else key[:10],
                "ts": iso_utc(ts) if ts else key + ":00:00+00:00",
                "segments": bars_map[key],
            }
        )
    return {"bars": bars}


def query_window_detail(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str,
    window_start: "datetime",
    window_end: "datetime",
    days: float | None = None,
) -> dict:
    """Return fill_by_model (quota_snapshots per model) and by_model (rollup) for one window.

    fill_by_model is a list of {model_id, series: [{ts, pct_used}]}, one entry per distinct
    model_id in quota_snapshots.  Providers with a single all-up quota (model_id="") produce
    one entry with model_id=""; providers with per-model quotas (e.g. Gemini flash/pro) produce
    one entry per model.  The frontend labels entries with model_id if multiple exist.

    When `days` is provided the snapshot lookup window is additionally clamped to
    `now - days` so the fill series matches the chart's visible time range.
    """
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from app.models.db import QuotaSnapshot

    snap_start = window_start
    if days is not None:
        since = datetime.now(UTC) - timedelta(days=days)
        snap_start = max(snap_start, since)

    snaps = session.exec(
        select(QuotaSnapshot)
        .where(
            QuotaSnapshot.provider_id == provider_id,
            QuotaSnapshot.account_id == account_id,
            QuotaSnapshot.window_type == window_type,
            QuotaSnapshot.ts >= snap_start,
            QuotaSnapshot.ts <= window_end,
        )
        .order_by(QuotaSnapshot.ts)  # type: ignore[arg-type]
    ).all()

    # Deduplicate snapshots to a reasonable number of points per window type.
    # Polls fire every ~30s so raw data is very dense; we bucket to avoid
    # returning hundreds of identical rows.
    #   session  → 30-min buckets  (a few-hour window → ~6–12 pts)
    #   daily    → 1-hour buckets  (24h window → ≤24 pts)
    #   weekly   → 6-hour buckets  (7d window → ≤28 pts)
    #   monthly+ → 1-day buckets   (30d window → ≤30 pts)
    _bucket_seconds = {
        "session": 1800,
        "daily": 3600,
        "weekly": 21600,
    }.get(window_type, 86400)

    def _bucket_key(ts: "datetime") -> int:
        epoch = int(ts.timestamp())
        return epoch - (epoch % _bucket_seconds)

    by_model_snaps: dict[str, dict[int, QuotaSnapshot]] = {}
    for s in snaps:
        mid = s.model_id or ""
        if mid not in by_model_snaps:
            by_model_snaps[mid] = {}
        bk = _bucket_key(s.ts)
        by_model_snaps[mid][bk] = s  # last snapshot in each bucket wins

    fill_by_model = []
    for mid, bk_map in sorted(by_model_snaps.items()):
        series = [
            {
                "ts": s.ts.isoformat() if s.ts.tzinfo else s.ts.isoformat() + "+00:00",
                "pct_used": s.pct_used,
            }
            for s in sorted(bk_map.values(), key=lambda x: x.ts)
        ]
        fill_by_model.append({"model_id": mid, "series": series})

    # Keep fill_series as a backwards-compat alias: all-up (model_id="") if present,
    # otherwise the first model's series.
    if "" in by_model_snaps:
        fill_series = [
            {
                "ts": s.ts.isoformat() if s.ts.tzinfo else s.ts.isoformat() + "+00:00",
                "pct_used": s.pct_used,
            }
            for s in sorted(by_model_snaps[""].values(), key=lambda x: x.ts)
        ]
    elif fill_by_model:
        fill_series = fill_by_model[0]["series"]  # type: ignore[assignment]
    else:
        fill_series = []

    start_key = window_start.strftime("%Y-%m-%d")
    end_key = window_end.strftime("%Y-%m-%d")

    rollup_rows = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == provider_id,
            UsagePeriodRollup.account_id == account_id,
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.period_key >= start_key,
            UsagePeriodRollup.period_key <= end_key,
            UsagePeriodRollup.model_id != "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).all()

    model_agg: dict[str, dict] = {}
    for r in rollup_rows:
        m = r.model_id
        if m not in model_agg:
            model_agg[m] = {"model_id": m, "tokens": 0, "cost_usd": 0.0, "msgs": 0}
        model_agg[m]["tokens"] += (
            r.tokens_input
            + r.tokens_output
            + r.tokens_cache_read
            + r.tokens_cache_create
            + r.tokens_reasoning
        )
        model_agg[m]["cost_usd"] += r.cost_usd
        model_agg[m]["msgs"] += r.msgs

    by_model = sorted(model_agg.values(), key=lambda x: x["tokens"], reverse=True)
    return {"fill_series": fill_series, "fill_by_model": fill_by_model, "by_model": by_model}
