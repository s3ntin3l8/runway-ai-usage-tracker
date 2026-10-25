"""Cross-provider session ranking over usage_events.

The session analog of ``query_top_models``: groups by ``session_id`` so the
question "which session drove that spike" has a single answer, without
scoping to one provider+account the way ``query_sessions`` does. A session is
identified by its id alone — the same id can carry events from more than one
provider, so the row also reports the distinct providers that touched it.

Rows with a NULL or empty ``session_id`` (providers that never report one, or
report it blank) are excluded: they are not a session and cannot be ranked as
one. The empty case matters — without it every such event collapses into one
phantom session whose label is an empty id.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from app.models._datetime import iso_utc
from app.services.queries._shared import RankedRows, _parse_ts, sqlite_utc_timestamp


def rank_top_sessions(
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
    """Rank ``session_id`` across all providers/accounts in ``[since, until)``.

    Each row carries the session's ``project`` / ``cwd`` / ``sidecar_id`` — the
    host that pushed its **latest** event, not a lexicographic maximum (falling
    back to the session's best value when that latest event carries none) — its
    first and last event timestamp, the token component sums +
    ``tokens_total``, ``cost_usd`` + ``cost_cache``, ``msgs``, the number of
    distinct ``hosts`` the session ran on, and the distinct ``providers`` /
    ``models`` that contributed. ``metric`` ("tokens" | "cost") chooses the
    sort key; ``exclude_cache`` drops cache from that sort key only — the raw
    component fields stay populated so the frontend can recompute either view.

    The sort key is wrapped in SUM() because this query aggregates with
    GROUP BY — a bare column would order by an arbitrary row's value.

    The describing columns (``project``, ``cwd``, ``sidecar_id``) come from the
    session's latest event, because a session can move: it may start on one
    host and finish on another, and MAX() would report the alphabetically
    greatest of them while the UI reads the value as "the host that pushed
    this". ``ranked`` numbers each session's events newest-first and the
    aggregate keeps row 1's values.

    ``total`` is the number of ranked groups before the ``LIMIT`` truncated
    the result — how many sessions the ranking had, not how many it shows.
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
        WITH ranked AS (
            SELECT
                session_id, id, ts, project, cwd, sidecar_id, model_id,
                provider_id, event_id, turn_id,
                tokens_input, tokens_output, tokens_cache_read,
                tokens_cache_create, tokens_reasoning,
                cost_usd, cost_input, cost_output,
                cost_cache_read, cost_cache_create,
                -- Newest first within the session, so row 1 is the session's
                -- latest event. `id` breaks a tie on `ts` deterministically.
                ROW_NUMBER() OVER (
                    PARTITION BY session_id
                    ORDER BY ts DESC, id DESC
                ) AS rn
            FROM usage_events
            WHERE kind = 'message'
              AND session_id IS NOT NULL
              AND session_id != ''
              AND ts >= :since
              {until_clause}
              {provider_clause}
              {sidecar_clause}
        )
        SELECT
            session_id,
            -- The describing columns come from the session's latest event: a
            -- session can move between hosts, and MAX() would report the
            -- alphabetically greatest one while the UI reads the value as
            -- "the host that pushed this".
            --
            -- When the latest event carries no value of its own — an event
            -- with no cwd derives no project — fall back to the best value
            -- the session had. A label is worth more than the tidiness of
            -- taking the newest row's NULL at face value.
            COALESCE(MAX(CASE WHEN rn = 1 THEN project END), MAX(project)) AS project,
            COALESCE(MAX(CASE WHEN rn = 1 THEN cwd END), MAX(cwd))         AS cwd,
            COALESCE(MAX(CASE WHEN rn = 1 THEN sidecar_id END),
                     MAX(sidecar_id))                                     AS sidecar_id,
            MIN(ts)                                            AS ts_start,
            MAX(ts)                                            AS ts_end,
            COUNT(DISTINCT sidecar_id)                         AS hosts,
            -- The message count counts *turns*, not events: a provider that
            -- emits one event per API request (MiniMax Code) carries its own
            -- turn id, so its turns are counted distinctly. The key carries
            -- the provider because this query groups across providers, where
            -- `event_id` is only unique per `(provider_id, event_id)`.
            COUNT(DISTINCT provider_id || '|'
                || COALESCE(NULLIF(turn_id, ''), event_id))      AS msgs,
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
            GROUP_CONCAT(DISTINCT model_id)                     AS models_csv,
            -- The untruncated size of the ranking. A window function is
            -- evaluated after GROUP BY and before LIMIT, so this counts
            -- every group of the same filter set in the one scan that
            -- produces the rows — the count cannot disagree with them.
            COUNT(*) OVER ()                                    AS total_groups
        FROM ranked
        GROUP BY session_id
        ORDER BY {order_expr} DESC, ts_end DESC, session_id
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
        models = [m for m in (row.models_csv or "").split(",") if m]
        ts_start = _parse_ts(row.ts_start)
        ts_end = _parse_ts(row.ts_end)
        results.append(
            {
                "session_id": row.session_id,
                "project": row.project,
                "cwd": row.cwd,
                "sidecar_id": row.sidecar_id,
                "ts_start": iso_utc(ts_start) if ts_start else None,
                "ts_end": iso_utc(ts_end) if ts_end else None,
                "hosts": int(row.hosts or 0),
                "msgs": int(row.msgs or 0),
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
                "models": models,
            }
        )
    # The count rides on every row of the ranking; an empty ranking had no
    # groups to begin with.
    total = int(rows[0].total_groups) if rows else 0
    return RankedRows(rows=results, total=total)
