"""Most-used tools over a time range, from per-event tool_names.

Each event stores its tool_use block names as a JSON array in
``usage_events.tools_json``. SQLite's ``json_each`` unnests that array so we can
count tool invocations across many events. Only Anthropic populates tools_json
today, so the ranking reflects Claude tool usage — the query itself is
provider-agnostic.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from app.services.queries._shared import RankedRows, sqlite_utc_timestamp


def rank_top_tools(
    session: Session,
    *,
    since: datetime,
    until: datetime | None = None,
    provider_id: str | None = None,
    sidecar_id: str | None = None,
    limit: int = 15,
) -> RankedRows:
    """Rank tool names by invocation count in ``[since, until)``.

    Each row: ``tool`` (name), ``calls`` (total invocations), ``msgs`` (distinct
    messages that used it). Ordered by calls desc.

    ``total`` is the number of ranked groups before the ``LIMIT`` truncated
    the result — how many tools the ranking had, not how many it shows.
    """
    until_clause = "AND e.ts < :until" if until is not None else ""
    provider_clause = "AND e.provider_id = :provider_id" if provider_id else ""
    sidecar_clause = "AND e.sidecar_id = :sidecar_id" if sidecar_id else ""

    sql = text(
        f"""
        SELECT
            j.value          AS tool,
            COUNT(*)         AS calls,
            COUNT(DISTINCT e.id) AS msgs,
            -- The untruncated size of the ranking. A window function is
            -- evaluated after GROUP BY and before LIMIT, so this counts
            -- every group of the same filter set in the one scan that
            -- produces the rows — the count cannot disagree with them.
            COUNT(*) OVER () AS total_groups
        FROM usage_events e
        JOIN json_each(e.tools_json) j
        WHERE e.kind = 'message'
          AND e.tools_json IS NOT NULL
          -- An unnamed tool is not a tool. The SQL drops it as well as the
          -- Python filter below, so the ranked group set and the returned
          -- rows are the same set and the total counts exactly the tools
          -- that were ranked. A JSON null value fails this comparison too,
          -- which covers both.
          AND j.value != ''
          AND e.ts >= :since
          {until_clause}
          {provider_clause}
          {sidecar_clause}
        GROUP BY j.value
        ORDER BY calls DESC
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
    results = [
        {"tool": row.tool, "calls": int(row.calls or 0), "msgs": int(row.msgs or 0)}
        for row in rows
        if row.tool
    ]
    # The count rides on every row of the ranking; an empty ranking had no
    # groups to begin with.
    total = int(rows[0].total_groups) if rows else 0
    return RankedRows(rows=results, total=total)


def query_top_tools(
    session: Session,
    *,
    since: datetime,
    until: datetime | None = None,
    provider_id: str | None = None,
    sidecar_id: str | None = None,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """The rows of the tool ranking in ``[since, until)``, capped at ``limit``.

    See ``rank_top_tools`` for the ranking itself; this wrapper keeps the
    older, rows-only signature for the callers that need nothing else.
    """
    return rank_top_tools(
        session,
        since=since,
        until=until,
        provider_id=provider_id,
        sidecar_id=sidecar_id,
        limit=limit,
    ).rows
