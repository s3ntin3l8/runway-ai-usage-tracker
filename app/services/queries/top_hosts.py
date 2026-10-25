"""Cross-provider host ranking over usage_events.

The host analog of ``query_top_models``: groups by ``sidecar_id`` — the
hostname that pushed the event — so the question "which machine drove that
spike" has a single answer. The host is the only machine dimension the event
log carries; ``sidecar_registry`` owns the display names for those ids.

Rows with a NULL or empty ``sidecar_id`` are excluded: an event with no
pushing host cannot be attributed to one.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from app.models._datetime import iso_utc
from app.services.queries._shared import RankedRows, _parse_ts, sqlite_utc_timestamp


def rank_top_hosts(
    session: Session,
    *,
    since: datetime,
    until: datetime | None = None,
    metric: str = "tokens",
    exclude_cache: bool = False,
    provider_id: str | None = None,
    sidecar_id: str | None = None,
    limit: int = 15,
) -> RankedRows:
    """Rank ``sidecar_id`` across all providers/accounts in ``[since, until)``.

    Each row carries the host's ``sidecar_id``, the number of distinct
    ``sessions`` that ran on it, its first and last event timestamp, the
    token component sums + ``tokens_total``, ``cost_usd`` + ``cost_cache``,
    ``msgs``, and the distinct ``providers`` that pushed to it. ``metric``
    ("tokens" | "cost") chooses the sort key; ``exclude_cache`` drops cache
    from that sort key only — the raw component fields stay populated so the
    frontend can recompute either view.

    ``sidecar_id`` narrows the ranking to one host, matching the source
    filter the sibling rankings honour.

    The sort key is wrapped in SUM() because this query aggregates with
    GROUP BY — a bare column would order by an arbitrary row's value.

    ``total`` is the number of ranked groups before the ``LIMIT`` truncated
    the result — how many hosts the ranking had, not how many it shows.
    """
    until_clause = "AND ts < :until" if until is not None else ""
    provider_clause = "AND provider_id = :provider_id" if provider_id else ""
    sidecar_clause = "AND sidecar_id = :sidecar_id" if sidecar_id else ""

    if metric == "cost":
        order_expr = (
            "SUM(cost_usd - cost_cache_read - cost_cache_create)"
            if exclude_cache
            else "SUM(cost_usd)"
        )
    elif exclude_cache:
        order_expr = "SUM(tokens_input + tokens_output + tokens_reasoning)"
    else:
        order_expr = (
            "SUM(tokens_input + tokens_output + tokens_cache_read "
            "+ tokens_cache_create + tokens_reasoning)"
        )

    sql = text(
        f"""
        SELECT
            sidecar_id,
            MIN(ts)                                            AS ts_start,
            MAX(ts)                                            AS ts_end,
            -- The message count counts *turns*, not events: a provider that
            -- emits one event per API request (MiniMax Code) carries its own
            -- turn id, so its turns are counted distinctly. The key carries
            -- the provider because this query groups across providers, where
            -- `event_id` is only unique per `(provider_id, event_id)`.
            COUNT(DISTINCT provider_id || '|'
                || COALESCE(NULLIF(turn_id, ''), event_id))      AS msgs,
            COUNT(DISTINCT session_id)                          AS sessions,
            SUM(tokens_input + tokens_output
                + tokens_cache_read + tokens_cache_create
                + tokens_reasoning)                             AS tokens_total,
            SUM(tokens_input)                                   AS tokens_input,
            SUM(tokens_output)                                  AS tokens_output,
            SUM(tokens_cache_read)                              AS tokens_cache_read,
            SUM(tokens_cache_create)                            AS tokens_cache_create,
            SUM(tokens_reasoning)                               AS tokens_reasoning,
            SUM(cost_usd)                                       AS cost_usd,
            SUM(cost_cache_read + cost_cache_create)            AS cost_cache,
            SUM(cost_input)                                     AS cost_input,
            SUM(cost_output)                                    AS cost_output,
            SUM(cost_cache_read)                                AS cost_cache_read,
            SUM(cost_cache_create)                              AS cost_cache_create,
            GROUP_CONCAT(DISTINCT provider_id)                  AS providers_csv,
            -- The untruncated size of the ranking. A window function is
            -- evaluated after GROUP BY and before LIMIT, so this counts
            -- every group of the same filter set in the one scan that
            -- produces the rows — the count cannot disagree with them.
            COUNT(*) OVER ()                                    AS total_groups
        FROM usage_events
        WHERE kind = 'message'
          AND sidecar_id IS NOT NULL
          AND sidecar_id != ''
          AND ts >= :since
          {until_clause}
          {provider_clause}
          {sidecar_clause}
        GROUP BY sidecar_id
        ORDER BY {order_expr} DESC, ts_end DESC, sidecar_id
        LIMIT :limit
        """
    )

    params: dict[str, Any] = {"since": sqlite_utc_timestamp(since), "limit": limit}
    if until is not None:
        params["until"] = sqlite_utc_timestamp(until)
    if provider_id:
        params["provider_id"] = provider_id
    if sidecar_id:
        params["sidecar_id"] = sidecar_id

    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]

    results: list[dict[str, Any]] = []
    for row in rows:
        providers = [p for p in (row.providers_csv or "").split(",") if p]
        ts_start = _parse_ts(row.ts_start)
        ts_end = _parse_ts(row.ts_end)
        results.append(
            {
                "sidecar_id": row.sidecar_id,
                "ts_start": iso_utc(ts_start) if ts_start else None,
                "ts_end": iso_utc(ts_end) if ts_end else None,
                "msgs": int(row.msgs or 0),
                "sessions": int(row.sessions or 0),
                "tokens_total": int(row.tokens_total or 0),
                "tokens_input": int(row.tokens_input or 0),
                "tokens_output": int(row.tokens_output or 0),
                "tokens_cache_read": int(row.tokens_cache_read or 0),
                "tokens_cache_create": int(row.tokens_cache_create or 0),
                "tokens_reasoning": int(row.tokens_reasoning or 0),
                "cost_usd": float(row.cost_usd or 0.0),
                "cost_cache": float(row.cost_cache or 0.0),
                "cost_input": float(row.cost_input or 0.0),
                "cost_output": float(row.cost_output or 0.0),
                "cost_cache_read": float(row.cost_cache_read or 0.0),
                "cost_cache_create": float(row.cost_cache_create or 0.0),
                "providers": providers,
            }
        )
    # The count rides on every row of the ranking; an empty ranking had no
    # groups to begin with.
    total = int(rows[0].total_groups) if rows else 0
    return RankedRows(rows=results, total=total)


def query_top_hosts(
    session: Session,
    *,
    since: datetime,
    until: datetime | None = None,
    metric: str = "tokens",
    exclude_cache: bool = False,
    provider_id: str | None = None,
    sidecar_id: str | None = None,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """The rows of the host ranking in ``[since, until)``, capped at ``limit``.

    See ``rank_top_hosts`` for the ranking itself; this wrapper keeps the
    older, rows-only signature for the callers that need nothing else.
    """
    return rank_top_hosts(
        session,
        since=since,
        until=until,
        metric=metric,
        exclude_cache=exclude_cache,
        provider_id=provider_id,
        sidecar_id=sidecar_id,
        limit=limit,
    ).rows
