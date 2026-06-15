"""Cross-provider project ranking over usage_events.

The project analog of ``query_top_models``: groups by the working-directory
``project`` label (basename of cwd, server-derived at ingest) so one repo's
usage sums across every tool it was worked on from. With ``provider_id`` set it
scopes to a single provider (the per-provider Activity card); without it the
ranking spans all providers (the global History card). Rows with a NULL project
(unattributed / pre-backfill / non-logging providers) are excluded.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session


def query_top_projects(
    session: Session,
    *,
    since: datetime,
    until: datetime | None = None,
    metric: str = "tokens",
    exclude_cache: bool = False,
    provider_id: str | None = None,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Rank ``project`` in ``[since, until)`` by tokens, cost, or session count.

    Each row: ``project``, token component sums + ``tokens_total``, ``cost_usd``
    + ``cost_cache``, ``msgs``, ``sessions`` (distinct session_id), and
    ``providers`` (distinct providers that touched the project). ``metric``
    ("tokens" | "cost" | "sessions") chooses the sort key; ``exclude_cache``
    drops cache from the tokens/cost sort key only.
    """
    until_clause = "AND ts < :until" if until is not None else ""
    provider_clause = "AND provider_id = :provider_id" if provider_id else ""

    if metric == "cost":
        order_expr = (
            "SUM(cost_usd - cost_cache_read - cost_cache_create)"
            if exclude_cache
            else "SUM(cost_usd)"
        )
    elif metric == "sessions":
        order_expr = "COUNT(DISTINCT session_id)"
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
            project,
            COUNT(*)                                            AS msgs,
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
            GROUP_CONCAT(DISTINCT provider_id)                  AS providers_csv
        FROM usage_events
        WHERE kind = 'message'
          AND project IS NOT NULL
          AND ts >= :since
          {until_clause}
          {provider_clause}
        GROUP BY project
        ORDER BY {order_expr} DESC
        LIMIT :limit
        """
    )

    params: dict[str, Any] = {"since": since.isoformat(), "limit": limit}
    if until is not None:
        params["until"] = until.isoformat()
    if provider_id:
        params["provider_id"] = provider_id

    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]

    results: list[dict[str, Any]] = []
    for row in rows:
        providers = [p for p in (row.providers_csv or "").split(",") if p]
        results.append(
            {
                "project": row.project,
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
                "providers": providers,
            }
        )
    return results


def query_projects(
    session: Session,
    *,
    provider_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[str]:
    """Distinct non-NULL project labels (for the sessions filter dropdown)."""
    clauses = ["project IS NOT NULL"]
    params: dict[str, Any] = {}
    if provider_id:
        clauses.append("provider_id = :provider_id")
        params["provider_id"] = provider_id
    if since is not None:
        clauses.append("ts >= :since")
        params["since"] = since.isoformat()
    if until is not None:
        clauses.append("ts < :until")
        params["until"] = until.isoformat()
    where = " AND ".join(clauses)
    sql = text(f"SELECT DISTINCT project FROM usage_events WHERE {where} ORDER BY project")
    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]
    return [r.project for r in rows if r.project]
