"""Auto-extracted from app.services.event_query during the monolith split.
See app/services/queries/__init__.py for the public surface.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

logger = logging.getLogger(__name__)

from sqlalchemy import bindparam, text
from sqlmodel import Session, select

from app.core.date_utils import parse_iso8601_utc
from app.models._datetime import iso_utc
from app.models.db import UsageEvent, UsagePeriodRollup, UsageWindow
from app.services.queries._shared import _parse_period_key
from app.services.queries.windows import query_window_aggregation
from app.services.window_closer import WINDOW_DURATION

# Snapshot-bucket resolution for SQL downsampling. Pick the first tier whose
# threshold is >= the requested `days`. `quota_snapshots` is poll-rate granular
# (~30s per series), so without bucketing a 30-day window materialises ~860k
# rows just to return a 50-row page or a chart that visually fits a few hundred
# points.
_BUCKET_TIERS: list[tuple[float, int]] = [
    (0.1, 60),  # ≤ ~2.4h → 1-min
    (1.0, 300),  # ≤ 1d → 5-min
    (7.0, 1800),  # ≤ 7d → 30-min
    (30.0, 10800),  # ≤ 30d → 3-hour
    (90.0, 21600),  # ≤ 90d → 6-hour (keeps the weekly sawtooth visible)
    (float("inf"), 86400),  # else → 1-day
]


def _bucket_seconds_for(days: float) -> int:
    for threshold, secs in _BUCKET_TIERS:
        if days <= threshold:
            return secs
    return 86400  # unreachable: last tier matches inf


def _min_bucket(dt: datetime) -> datetime:
    """Truncate to the minute. reset_at/window_end timestamps differ by up to
    ~1s due to insertion jitter, so we match on minute buckets instead of
    relying on exact equality."""
    return dt.replace(second=0, microsecond=0)


@dataclass(slots=True)
class _ParsedRow:
    """Snapshot page row after datetime parsing. Mirrors the columns of
    `_SNAPSHOTS_PAGE_SQL` and is what `_build_window_stats_for_rows` accepts."""

    provider_id: str
    account_id: str
    window_type: str
    model_id: str
    ts: datetime | None
    reset_at: datetime | None
    pct_used: float | None
    delta: float | None


def _parse_sqlite_dt(value: object) -> datetime | None:
    """Coerce a value returned from a raw `text()` query into a naive UTC
    datetime. SQLAlchemy auto-decodes datetimes for ORM-mapped columns, but
    raw text queries return SQLite's storage format (TEXT with either 'T' or
    ' ' separator) as plain strings.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str):
        s = value.replace("T", " ")
        # SQLite occasionally tacks on trailing whitespace or timezone hints.
        s = s.strip().rstrip("Z")
        try:
            return datetime.fromisoformat(s).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _live_open_window_totals(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str,
    reset_dt: datetime | None,
    tokens_total: float | None,
    cost_usd: float | None,
) -> tuple[float | None, float | None, str | None]:
    """Fill in tokens_total/cost_usd/top_model for an open window row from a
    live aggregation over usage_events, when the card itself didn't carry
    them (e.g. the percent-only Anthropic quota card). Guarded because
    WINDOW_DURATION only covers session/daily/weekly/monthly — provider-
    specific window types (e.g. weekly_opus) would otherwise KeyError.
    """
    top_model: str | None = None
    if reset_dt is None or window_type not in WINDOW_DURATION:
        return tokens_total, cost_usd, top_model
    try:
        agg = query_window_aggregation(
            session,
            provider_id=provider_id,
            account_id=account_id,
            window_type=window_type,
            reset_at=reset_dt,
        )
    except (KeyError, ValueError):
        logger.debug("Failed to compute live window aggregation for open window", exc_info=True)
        return tokens_total, cost_usd, top_model

    if tokens_total is None:
        tokens_total = agg["token_usage"]["total"]
    if cost_usd is None:
        cost_usd = agg["cost_usd"]
    by_model = agg["by_model"]
    if by_model:
        top_model = max(
            by_model,
            key=lambda mid: (
                by_model[mid]["tokens_input"]
                + by_model[mid]["tokens_output"]
                + by_model[mid]["tokens_cache_read"]
                + by_model[mid]["tokens_cache_create"]
                + by_model[mid]["tokens_reasoning"]
            ),
        )
    return tokens_total, cost_usd, top_model


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
        reset_dt: datetime | None = None

        # Apply the days filter to open windows: skip if reset_at is older than `since`.
        # Windows with no reset_at are always current (e.g. session-scoped).
        if reset_at:
            try:
                reset_dt = parse_iso8601_utc(reset_at)
                if reset_dt < since:
                    continue
            except Exception:
                logger.debug("Failed to parse reset_at in snapshot window filter", exc_info=True)

        token_usage = card.get("token_usage") or {}
        base_name = card.get("service_name", lu.provider_id.capitalize())
        variant = lu.variant if lu.variant and lu.variant != "default" else None
        service_name = f"{base_name} · {variant}" if variant else base_name
        open_keys.add((lu.provider_id, lu.account_id, wt))

        tokens_total, cost_usd, top_model = _live_open_window_totals(
            session,
            provider_id=lu.provider_id,
            account_id=lu.account_id,
            window_type=wt,
            reset_dt=reset_dt,
            tokens_total=token_usage.get("total"),
            cost_usd=card.get("cost_usd"),
        )

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
                "tokens_total": tokens_total,
                "cost_usd": cost_usd,
                "msgs": card.get("msgs"),
                "top_model": top_model,
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


def _build_window_stats_for_rows(
    session: Session,
    page_rows: list,
    now_naive: datetime,
) -> dict[tuple, dict]:
    """Compute per-(pid, aid, wt, mid, minute_bucket(reset_at)) token/cost
    enrichment for the given page rows.

    Closed windows (reset_at <= now): look up matching `usage_windows` by
    minute-bucketed `window_end`.
    Open windows (reset_at > now): sum `usage_events` in [window_start, now].

    The input was previously every snapshot in the time range; bucketing
    pagination reduces it to ≤ `limit` rows, so the batched event query
    only scans the events touching the page's live windows.
    """
    window_stats: dict[tuple, dict] = {}

    past_resets = {r.reset_at for r in page_rows if r.reset_at and r.reset_at <= now_naive}
    if past_resets:
        min_t = min(past_resets) - timedelta(minutes=2)
        max_t = max(past_resets) + timedelta(minutes=2)
        for w in session.exec(
            select(UsageWindow).where(
                UsageWindow.window_end >= min_t,
                UsageWindow.window_end <= max_t,
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
            key = (
                w.provider_id,
                w.account_id,
                w.window_type,
                w.model_id,
                _min_bucket(w.window_end),
            )
            if key not in window_stats:
                window_stats[key] = {
                    "tokens_total": tokens or None,
                    "cost_usd": w.cost_usd or None,
                }

    # Open windows: reset_at > now. LatestUsage cards for quota-only providers
    # (e.g. Claude) carry no token/cost data, so we compute running totals
    # directly from events. Per-model rows filter by model_id; the aggregate
    # row (model_id="") sums across all models.
    actual_reset_lookup: dict[tuple, datetime] = {}
    for r in page_rows:
        if not (r.reset_at and r.reset_at > now_naive):
            continue
        key4 = (r.provider_id, r.account_id, r.window_type, r.model_id)
        actual_reset_lookup.setdefault(key4, r.reset_at)

    series_windows: dict[tuple, datetime] = {}
    series_by_account: dict[tuple, list[tuple[tuple, datetime, str]]] = {}
    for r in page_rows:
        if not (r.reset_at and r.reset_at > now_naive and r.window_type in WINDOW_DURATION):
            continue
        key4 = (r.provider_id, r.account_id, r.window_type, r.model_id)
        actual_reset = actual_reset_lookup.get(key4)
        if not actual_reset:
            continue
        window_start = actual_reset - WINDOW_DURATION[r.window_type]
        key = (*key4, _min_bucket(r.reset_at))
        if key in series_windows:
            continue
        series_windows[key] = window_start
        series_by_account.setdefault((r.provider_id, r.account_id), []).append(
            (key, window_start, r.model_id)
        )

    if series_windows:
        # Seed every series so quota-only windows with no events still appear
        # with {tokens_total: None, cost_usd: None} downstream.
        accumulators: dict[tuple, dict[str, float]] = {
            key: {"tokens": 0, "cost": 0.0} for key in series_windows
        }
        min_window_start = min(series_windows.values())
        pids = {pid for pid, _ in series_by_account}
        aids = {aid for _, aid in series_by_account}
        events = session.exec(
            select(UsageEvent).where(
                UsageEvent.ts >= min_window_start,
                UsageEvent.ts <= now_naive,
                UsageEvent.provider_id.in_(pids),  # type: ignore[attr-defined]
                UsageEvent.account_id.in_(aids),  # type: ignore[attr-defined]
            )
        ).all()
        for ev in events:
            for key, window_start, mid in series_by_account.get(
                (ev.provider_id, ev.account_id), ()
            ):
                if mid and ev.model_id != mid:
                    continue
                if ev.ts < window_start:
                    continue
                acc = accumulators[key]
                acc["tokens"] += (
                    (ev.tokens_input or 0)
                    + (ev.tokens_output or 0)
                    + (ev.tokens_cache_read or 0)
                    + (ev.tokens_cache_create or 0)
                    + (ev.tokens_reasoning or 0)
                )
                acc["cost"] += ev.cost_usd or 0.0
        for key, totals in accumulators.items():
            window_stats[key] = {
                "tokens_total": totals["tokens"] or None,
                "cost_usd": totals["cost"] or None,
            }

    return window_stats


# SQL passes for query_snapshots. The bucket expression `strftime('%s', ts) /
# :bucket_seconds` is integer division (SQLite int math) — every snapshot in
# the same `(series, bucket)` partition gets the same bucket key.
#
# Pass 1 counts the (series, bucket) cardinality after filters; pass 2 picks
# the latest snapshot per bucket via ROW_NUMBER, then computes per-series
# delta with LAG. LAG ignores gaps — the delta on bucket N is computed against
# the previous *existing* bucket for that series, not the previous calendar
# bucket. This matches the prior Python behavior (it iterated existing rows).
# `provider_ids` is an expanding bind param so a single SQL string can serve
# both the "no filter" path (has_provider_filter=0, IN clause short-circuited)
# and the "filter to N providers" path (has_provider_filter=1, IN matches the
# bound list). COUNT and PAGE must use identical filter clauses — drift here
# causes the pager total to mismatch the visible rows.
_SNAPSHOTS_COUNT_SQL = text(
    # Counts only buckets whose latest snapshot has a non-zero pct_used (NULL
    # is preserved so "unknown %" rows still surface). This matches the rows
    # the page query will actually return — without the filter, pagination
    # totals included zero rows that the user never sees, producing empty
    # late pages.
    """
    WITH bucketed AS (
        SELECT
            pct_used,
            ROW_NUMBER() OVER (
                PARTITION BY provider_id, account_id, window_type, model_id,
                             (CAST(strftime('%s', ts) AS INTEGER) / :bucket_seconds)
                ORDER BY ts DESC
            ) AS rn
        FROM quota_snapshots
        WHERE ts >= :since
          AND (:has_provider_filter = 0 OR provider_id IN :provider_ids)
          AND (:account_id  IS NULL OR account_id  = :account_id)
          AND (:window_type IS NULL OR window_type = :window_type)
    )
    SELECT COUNT(*) AS n FROM bucketed
    WHERE rn = 1 AND (pct_used IS NULL OR pct_used > 0)
    """
).bindparams(bindparam("provider_ids", expanding=True))

_CHART_PERCENT_SQL = text(
    # Bucket aggregate is MAX(pct_used), not the latest sample in the bucket.
    # A daily-resetting quota whose reset falls near the end of the UTC bucket
    # (e.g. Gemini Pro at ~21:08 UTC) would otherwise read 0 for the whole day —
    # peak captures the actual usage the user is asking the chart about.
    """
    SELECT
        MIN(ts)       AS ts,
        MAX(pct_used) AS pct_used,
        provider_id, account_id, window_type, model_id
    FROM quota_snapshots
    WHERE ts >= :since
      AND pct_used IS NOT NULL
      AND (:provider_id IS NULL OR provider_id = :provider_id)
      AND (:account_id  IS NULL OR account_id  = :account_id)
    GROUP BY provider_id, account_id, window_type, model_id,
             (CAST(strftime('%s', ts) AS INTEGER) / :bucket_seconds)
    ORDER BY provider_id, window_type, model_id, ts ASC
    """
)


_SNAPSHOTS_PAGE_SQL = text(
    """
    WITH bucketed AS (
        SELECT
            ts, pct_used, reset_at,
            provider_id, account_id, window_type, model_id,
            ROW_NUMBER() OVER (
                PARTITION BY provider_id, account_id, window_type, model_id,
                             (CAST(strftime('%s', ts) AS INTEGER) / :bucket_seconds)
                ORDER BY ts DESC
            ) AS rn
        FROM quota_snapshots
        WHERE ts >= :since
          AND (:has_provider_filter = 0 OR provider_id IN :provider_ids)
          AND (:account_id  IS NULL OR account_id  = :account_id)
          AND (:window_type IS NULL OR window_type = :window_type)
    ),
    last_per_bucket AS (
        SELECT ts, pct_used, reset_at,
               provider_id, account_id, window_type, model_id
        FROM bucketed
        WHERE rn = 1
    ),
    with_delta AS (
        SELECT ts, pct_used, reset_at,
               provider_id, account_id, window_type, model_id,
               LAG(pct_used) OVER (
                   PARTITION BY provider_id, account_id, window_type, model_id
                   ORDER BY ts ASC
               ) AS prev_pct
        FROM last_per_bucket
    )
    SELECT
        ts, pct_used, reset_at,
        provider_id, account_id, window_type, model_id,
        CASE
            WHEN prev_pct IS NULL OR pct_used IS NULL THEN NULL
            ELSE ROUND(pct_used - prev_pct, 2)
        END AS delta
    FROM with_delta
    -- Skip rows the table view would have hidden anyway. The LAG in with_delta
    -- still runs over the unfiltered bucketed series, so deltas remain
    -- "vs. previous existing bucket" (zero rows just don't surface).
    WHERE pct_used IS NULL OR pct_used > 0
    ORDER BY ts DESC
    LIMIT :limit OFFSET :offset
    """
).bindparams(bindparam("provider_ids", expanding=True))


def query_snapshots(
    session: Session,
    *,
    provider_ids: list[str] | None = None,
    account_id: str | None = None,
    window_type: str | None = None,
    days: float = 7.0,
    page: int = 1,
    limit: int = 100,
) -> dict:
    """Flat paginated list of quota_snapshots, newest first, with per-series delta.

    Snapshots are bucketed by time (see `_BUCKET_TIERS`) so we never materialise
    more than (series × buckets) rows even on long timeframes. Pagination
    happens in SQL; enrichment (tokens_total / cost_usd) runs only against
    the page's rows.
    """
    # QuotaSnapshot.ts/reset_at are stored naive — bind naive UTC to match.
    since = (datetime.now(UTC) - timedelta(days=days)).replace(tzinfo=None)
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    bucket_seconds = _bucket_seconds_for(days)
    wt_param = window_type if window_type and window_type != "all" else None
    # Expanding bindparam doesn't accept empty lists. When no filter is active
    # we bind a dummy value and short-circuit via has_provider_filter=0.
    has_provider_filter = 1 if provider_ids else 0
    pids_param = list(provider_ids) if provider_ids else [""]
    params = {
        "since": since,
        "has_provider_filter": has_provider_filter,
        "provider_ids": pids_param,
        "account_id": account_id,
        "window_type": wt_param,
        "bucket_seconds": bucket_seconds,
        "limit": limit,
        "offset": max(0, (page - 1) * limit),
    }

    total_row = session.exec(_SNAPSHOTS_COUNT_SQL, params=params).one()  # type: ignore[call-overload]
    total = int(total_row.n or 0)

    page_rows = session.exec(_SNAPSHOTS_PAGE_SQL, params=params).all()  # type: ignore[call-overload]

    # Raw text() queries return ts/reset_at as plain strings; normalise once
    # so downstream code (_build_window_stats_for_rows and ISO emission) can
    # work in datetime-land like the rest of the codebase.
    parsed_rows = []
    for r in page_rows:
        parsed_rows.append(
            _ParsedRow(
                provider_id=r.provider_id,
                account_id=r.account_id,
                window_type=r.window_type,
                model_id=r.model_id,
                ts=_parse_sqlite_dt(r.ts),
                reset_at=_parse_sqlite_dt(r.reset_at),
                pct_used=r.pct_used,
                delta=r.delta,
            )
        )

    window_stats = _build_window_stats_for_rows(session, parsed_rows, now_naive)

    rows: list[dict] = []
    for r in parsed_rows:
        pid, aid, wt, mid = r.provider_id, r.account_id, r.window_type, r.model_id
        service = _PROVIDER_LABELS.get(pid, pid.capitalize())
        model_label = mid.capitalize() if mid else "-"
        ts_iso = r.ts.isoformat() + "+00:00" if r.ts is not None else None
        reset_iso = r.reset_at.isoformat() + "+00:00" if r.reset_at else None
        stats = (
            window_stats.get((pid, aid, wt, mid, _min_bucket(r.reset_at)), {}) if r.reset_at else {}
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
                "pct_used": r.pct_used,
                "delta": r.delta,
                "reset_at": reset_iso,
                "tokens_total": stats.get("tokens_total"),
                "cost_usd": stats.get("cost_usd"),
            }
        )

    return {"total": total, "page": page, "limit": limit, "rows": rows}


def query_chart(  # noqa: PLR0915 — known-debt: multi-metric chart aggregator, splits poorly
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = 30.0,
    metric: str = "percent",
    since: datetime | None = None,
    until: datetime | None = None,
    group: str | None = None,
) -> dict:
    """Return chart data.

    metric=percent  → fill curves from quota_snapshots.
    metric=tokens   → daily bars from usage_period_rollup.
    metric=cost     → daily bars (value=cost_usd) from usage_period_rollup.

    By default the range is the last `days`. Passing an explicit `since` (and
    optional exclusive `until`) scopes the chart to a closed period (e.g. a
    selected past month); an `until` also forces daily-bar granularity so a
    full month renders as day bars rather than hourly.

    group="provider" collapses the token/cost segments to one per provider per
    bar (summing across accounts and models) — the cross-provider "overall"
    view. With no group, each bar keeps one segment per (provider, model).
    """
    import json as _json

    explicit_range = since is not None
    if since is None:
        since = datetime.now(UTC) - timedelta(days=days)

    if metric == "percent":
        # Bucket snapshots by time at the SQL layer to keep chart resolution
        # matched to the timeframe (see `_BUCKET_TIERS`). The `pct_used IS NOT
        # NULL` filter lives inside the bucketed CTE so a null row can never
        # be selected as the bucket representative.
        bucket_seconds = _bucket_seconds_for(days)
        chart_params = {
            "since": since.replace(tzinfo=None),
            "provider_id": provider_id,
            "account_id": account_id,
            "bucket_seconds": bucket_seconds,
        }
        snaps = session.exec(_CHART_PERCENT_SQL, params=chart_params).all()  # type: ignore[call-overload]

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
            ts_dt = _parse_sqlite_dt(s.ts)
            ts_iso = ts_dt.isoformat() + "+00:00" if ts_dt is not None else str(s.ts)
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

    # tokens or cost — hourly bars for short rolling windows, daily bars
    # otherwise. A scoped range (explicit since/until, e.g. a whole month) always
    # renders as daily bars.
    period_type = "day" if (explicit_range or days > 7) else "hour"
    since_key = (
        since.strftime("%Y-%m-%dT%H") if period_type == "hour" else since.strftime("%Y-%m-%d")
    )
    bar_stmt = select(UsagePeriodRollup).where(
        UsagePeriodRollup.period_type == period_type,
        UsagePeriodRollup.period_key >= since_key,
        UsagePeriodRollup.sidecar_id == "",
    )
    if until is not None:
        # period_key is exclusive-upper-bound friendly: a 'YYYY-MM-DD' key sorts
        # before the until day's key, so "< until_key" drops the boundary day.
        until_key = (
            until.strftime("%Y-%m-%dT%H") if period_type == "hour" else until.strftime("%Y-%m-%d")
        )
        bar_stmt = bar_stmt.where(UsagePeriodRollup.period_key < until_key)
    if provider_id:
        bar_stmt = bar_stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        bar_stmt = bar_stmt.where(UsagePeriodRollup.account_id == account_id)

    all_bar_rows = list(session.exec(bar_stmt.order_by(UsagePeriodRollup.period_key)).all())
    # (provider, account, period) tuples that have per-model rows — used to skip
    # their aggregate (model_id="") row so it isn't double-counted. Keyed by
    # account too: an account's aggregate row must survive even when a *sibling*
    # account of the same provider has per-model rows (matters cross-provider /
    # unfiltered, where rows from multiple accounts share a bar).
    has_per_model: set[tuple[str, str, str]] = {
        (r.provider_id, r.account_id, r.period_key) for r in all_bar_rows if r.model_id != ""
    }

    by_provider = group == "provider"
    bars_map: dict[str, list] = {}
    # When grouping by provider, segments accumulate per (period, provider) so
    # multiple accounts/models of one provider sum into a single bar segment.
    provider_seg: dict[tuple[str, str], dict[str, object]] = {}
    for r in all_bar_rows:
        if r.model_id == "" and (r.provider_id, r.account_id, r.period_key) in has_per_model:
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
        # value_cache = the cache portion of `value`, so the client can subtract it
        # under the exclude-cache toggle. Its unit follows the metric: cache *tokens*
        # for the tokens bars, cache *cost* (USD) for the cost bars.
        value_cache = (
            r.cost_cache_read + r.cost_cache_create
            if metric == "cost"
            else r.tokens_cache_read + r.tokens_cache_create
        )

        if by_provider:
            seg_key = (key, r.provider_id)
            seg = provider_seg.get(seg_key)
            if seg is None:
                seg = {
                    "provider_id": r.provider_id,
                    "model_id": "",
                    "label": r.provider_id.capitalize(),
                    "value": 0.0,
                    "value_cache": 0.0,
                }
                provider_seg[seg_key] = seg
                bars_map[key].append(seg)
            seg["value"] = cast(float, seg["value"]) + value
            seg["value_cache"] = cast(float, seg["value_cache"]) + value_cache
            continue

        label = r.provider_id.capitalize()
        if use_model:
            label += f" · {use_model}"
        segment: dict[str, object] = {
            "provider_id": r.provider_id,
            "model_id": use_model,
            "label": label,
            "value": value,
            "value_cache": value_cache,
        }
        bars_map[key].append(segment)

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
