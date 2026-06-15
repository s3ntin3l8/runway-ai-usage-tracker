"""Auto-extracted from app.services.event_query during the monolith split.
See app/services/queries/__init__.py for the public surface.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from app.models._datetime import iso_utc
from app.services.queries._shared import _parse_ts

# ---------------------------------------------------------------------------
# 7.4  query_sessions
# ---------------------------------------------------------------------------


def query_sessions(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "tokens",
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Return top-N sessions by total tokens, newest first within the window.

    Each row includes ts_start, ts_end, duration_seconds, msgs, models[],
    by_model[], tokens_total, tokens_input, tokens_output, tokens_cache,
    cache_hit_pct, cost_usd, sidecar_id, project, cwd, git_branch.

    by_model is a list of per-model aggregates ordered by tokens_total DESC,
    covering all events with a non-NULL model_id in the session.

    `project` filters to sessions whose events carry that project label.
    `offset` pages the result (for the paginated Sessions browser).

    Events with NULL session_id are excluded.
    """
    if since is None:
        since = datetime.now(UTC) - timedelta(days=7)

    order_clause = "ts_end DESC" if sort_by == "recent" else "tokens_total DESC"
    # Exclusive upper bound — only emitted when an `until` is supplied so the
    # default (open-ended) window is byte-identical to the original query.
    until_clause = "AND ts < :until" if until is not None else ""
    project_clause = "AND project = :project" if project is not None else ""

    # Main aggregation query
    agg_sql = text(
        f"""
        SELECT
            session_id,
            MIN(ts)                                            AS ts_start,
            MAX(ts)                                            AS ts_end,
            COUNT(*)                                           AS msgs,
            SUM(tokens_input + tokens_output
                + tokens_cache_read + tokens_cache_create
                + tokens_reasoning)                            AS tokens_total,
            SUM(tokens_input)                                  AS tokens_input,
            SUM(tokens_output)                                 AS tokens_output,
            SUM(tokens_cache_read)                             AS tokens_cache_read,
            SUM(tokens_cache_create)                           AS tokens_cache_create,
            SUM(tokens_reasoning)                              AS tokens_reasoning,
            SUM(cost_usd)                                      AS cost_usd,
            SUM(cost_input)                                    AS cost_input,
            SUM(cost_output)                                   AS cost_output,
            SUM(cost_cache_read)                               AS cost_cache_read,
            SUM(cost_cache_create)                             AS cost_cache_create,
            SUM(tool_calls)                                    AS tool_calls,
            MAX(sidecar_id)                                    AS sidecar_id,
            MAX(project)                                       AS project,
            MAX(cwd)                                           AS cwd,
            MAX(git_branch)                                    AS git_branch,
            GROUP_CONCAT(DISTINCT model_id)                    AS models_csv,
            SUM(CASE WHEN subagent_type IS NOT NULL THEN 1 ELSE 0 END) AS subagent_msgs
        FROM usage_events
        WHERE provider_id = :provider_id
          AND account_id  = :account_id
          AND session_id IS NOT NULL
          AND ts >= :since
          {until_clause}
          {project_clause}
        GROUP BY session_id
        ORDER BY {order_clause}
        LIMIT :limit OFFSET :offset
        """
    )

    subagent_sql = text(
        """
        SELECT
            subagent_type,
            COUNT(*)                                            AS turns,
            SUM(tokens_input + tokens_output
                + tokens_cache_read + tokens_cache_create
                + tokens_reasoning)                             AS tokens_total,
            SUM(tokens_input)                                   AS tokens_input,
            SUM(tokens_output)                                  AS tokens_output,
            SUM(tokens_cache_read)                              AS tokens_cache_read,
            SUM(tokens_cache_create)                            AS tokens_cache_create,
            SUM(tokens_reasoning)                               AS tokens_reasoning,
            SUM(tool_calls)                                     AS tool_calls,
            SUM(cost_usd)                                       AS cost_usd,
            SUM(cost_input)                                     AS cost_input,
            SUM(cost_output)                                    AS cost_output,
            SUM(cost_cache_read)                                AS cost_cache_read,
            SUM(cost_cache_create)                              AS cost_cache_create
        FROM usage_events
        WHERE provider_id = :provider_id
          AND account_id  = :account_id
          AND session_id  = :session_id
          AND subagent_type IS NOT NULL
        GROUP BY subagent_type
        ORDER BY turns DESC
        """
    )

    model_breakdown_sql = text(
        """
        SELECT
            model_id,
            COUNT(*)                                            AS msgs,
            SUM(tokens_input + tokens_output
                + tokens_cache_read + tokens_cache_create
                + tokens_reasoning)                             AS tokens_total,
            SUM(tokens_input)                                   AS tokens_input,
            SUM(tokens_output)                                  AS tokens_output,
            SUM(tokens_cache_read)                              AS tokens_cache_read,
            SUM(tokens_cache_create)                            AS tokens_cache_create,
            SUM(tokens_reasoning)                               AS tokens_reasoning,
            SUM(tool_calls)                                     AS tool_calls,
            SUM(cost_usd)                                       AS cost_usd,
            SUM(cost_input)                                     AS cost_input,
            SUM(cost_output)                                    AS cost_output,
            SUM(cost_cache_read)                                AS cost_cache_read,
            SUM(cost_cache_create)                              AS cost_cache_create
        FROM usage_events
        WHERE provider_id = :provider_id
          AND account_id  = :account_id
          AND session_id  = :session_id
          AND model_id IS NOT NULL
        GROUP BY model_id
        ORDER BY tokens_total DESC
        """
    )

    rows = session.exec(  # type: ignore[call-overload]
        agg_sql,
        params={
            "provider_id": provider_id,
            "account_id": account_id,
            "since": since.isoformat(),
            **({"until": until.isoformat()} if until is not None else {}),
            **({"project": project} if project is not None else {}),
            "limit": limit,
            "offset": offset,
        },
    ).all()

    results: list[dict[str, Any]] = []
    for row in rows:
        ts_start = _parse_ts(row.ts_start)
        ts_end = _parse_ts(row.ts_end)
        duration = int((ts_end - ts_start).total_seconds()) if ts_start and ts_end else 0

        # Parse model list from GROUP_CONCAT result
        models: list[str] = []
        if row.models_csv:
            models = [m for m in row.models_csv.split(",") if m]

        tokens_input = int(row.tokens_input or 0)
        tokens_output = int(row.tokens_output or 0)
        cache_read = int(row.tokens_cache_read or 0)
        cache_create = int(row.tokens_cache_create or 0)
        tokens_reasoning = int(row.tokens_reasoning or 0)
        tokens_total = int(row.tokens_total or 0)
        tokens_cache = cache_read + cache_create
        cache_pct = round(tokens_cache / tokens_total * 100) if tokens_total > 0 else 0

        subagent_msgs = int(row.subagent_msgs or 0)
        subagents: list[dict[str, Any]] = []
        if subagent_msgs > 0:
            sa_rows = session.exec(  # type: ignore[call-overload]
                subagent_sql,
                params={
                    "provider_id": provider_id,
                    "account_id": account_id,
                    "session_id": row.session_id,
                },
            ).all()
            subagents = [
                {
                    "subagent_type": sa.subagent_type,
                    "turns": int(sa.turns or 0),
                    "tokens_total": int(sa.tokens_total or 0),
                    "tokens_input": int(sa.tokens_input or 0),
                    "tokens_output": int(sa.tokens_output or 0),
                    "tokens_cache_read": int(sa.tokens_cache_read or 0),
                    "tokens_cache_create": int(sa.tokens_cache_create or 0),
                    "tokens_reasoning": int(sa.tokens_reasoning or 0),
                    "tool_calls": int(sa.tool_calls or 0),
                    "cost_usd": float(sa.cost_usd or 0.0),
                    "cost_input": float(sa.cost_input or 0.0),
                    "cost_output": float(sa.cost_output or 0.0),
                    "cost_cache_read": float(sa.cost_cache_read or 0.0),
                    "cost_cache_create": float(sa.cost_cache_create or 0.0),
                }
                for sa in sa_rows
            ]

        by_model: list[dict[str, Any]] = []
        if models:
            bm_rows = session.exec(  # type: ignore[call-overload]
                model_breakdown_sql,
                params={
                    "provider_id": provider_id,
                    "account_id": account_id,
                    "session_id": row.session_id,
                },
            ).all()
            by_model = [
                {
                    "model_id": bm.model_id,
                    "msgs": int(bm.msgs or 0),
                    "tokens_total": int(bm.tokens_total or 0),
                    "tokens_input": int(bm.tokens_input or 0),
                    "tokens_output": int(bm.tokens_output or 0),
                    "tokens_cache_read": int(bm.tokens_cache_read or 0),
                    "tokens_cache_create": int(bm.tokens_cache_create or 0),
                    "tokens_reasoning": int(bm.tokens_reasoning or 0),
                    "tool_calls": int(bm.tool_calls or 0),
                    "cost_usd": float(bm.cost_usd or 0.0),
                    "cost_input": float(bm.cost_input or 0.0),
                    "cost_output": float(bm.cost_output or 0.0),
                    "cost_cache_read": float(bm.cost_cache_read or 0.0),
                    "cost_cache_create": float(bm.cost_cache_create or 0.0),
                }
                for bm in bm_rows
            ]

        results.append(
            {
                "session_id": row.session_id,
                "ts_start": iso_utc(ts_start),
                "ts_end": iso_utc(ts_end),
                "duration_seconds": duration,
                "msgs": int(row.msgs),
                "models": models,
                "by_model": by_model,
                "tokens_total": tokens_total,
                "tokens_input": tokens_input,
                "tokens_output": tokens_output,
                "tokens_cache_read": cache_read,
                "tokens_cache_create": cache_create,
                "tokens_cache": tokens_cache,
                "tokens_reasoning": tokens_reasoning,
                "tool_calls": int(row.tool_calls or 0),
                "cache_pct": cache_pct,
                "cost_usd": float(row.cost_usd or 0.0),
                "cost_input": float(row.cost_input or 0.0),
                "cost_output": float(row.cost_output or 0.0),
                "cost_cache_read": float(row.cost_cache_read or 0.0),
                "cost_cache_create": float(row.cost_cache_create or 0.0),
                "sidecar_id": row.sidecar_id,
                "project": row.project,
                "cwd": row.cwd,
                "git_branch": row.git_branch,
                "subagent_msgs": subagent_msgs,
                "subagents": subagents,
            }
        )

    return results


def count_sessions(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    project: str | None = None,
) -> int:
    """COUNT(DISTINCT session_id) for the same window/filters as query_sessions."""
    if since is None:
        since = datetime.now(UTC) - timedelta(days=7)
    until_clause = "AND ts < :until" if until is not None else ""
    project_clause = "AND project = :project" if project is not None else ""
    sql = text(
        f"""
        SELECT COUNT(DISTINCT session_id) AS n
        FROM usage_events
        WHERE provider_id = :provider_id
          AND account_id  = :account_id
          AND session_id IS NOT NULL
          AND ts >= :since
          {until_clause}
          {project_clause}
        """
    )
    row = session.exec(  # type: ignore[call-overload]
        sql,
        params={
            "provider_id": provider_id,
            "account_id": account_id,
            "since": since.isoformat(),
            **({"until": until.isoformat()} if until is not None else {}),
            **({"project": project} if project is not None else {}),
        },
    ).first()
    return int(row.n or 0) if row else 0


def query_sessions_paginated(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 0,
    page_size: int = 25,
    sort_by: str = "recent",
    project: str | None = None,
) -> dict[str, Any]:
    """One page of sessions + the total count, for the Sessions browser tab."""
    total = count_sessions(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since,
        until=until,
        project=project,
    )
    sessions = query_sessions(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since,
        until=until,
        limit=page_size,
        offset=page * page_size,
        sort_by=sort_by,
        project=project,
    )
    return {"sessions": sessions, "total": total, "limit": page_size, "offset": page * page_size}
