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
          -- An unnamed tool is not a tool, and a tool name that is not a
          -- string is not a name. This predicate is the ONLY filter: a
          -- second one in Python would drop rows the count had already
          -- included, and the total would then exceed the rows. `json_each`
          -- hands back a JSON false as the integer 0, which is not equal to
          -- '' and so would pass a plain inequality — hence the type check.
          -- A JSON null fails both.
          AND typeof(j.value) = 'text'
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
    # The SQL filter is the only filter, so every row here is a ranked group
    # and the count below covers exactly these rows.
    results = [
        {"tool": row.tool, "calls": int(row.calls or 0), "msgs": int(row.msgs or 0)} for row in rows
    ]
    # The count rides on every row of the ranking; an empty ranking had no
    # groups to begin with.
    total = int(rows[0].total_groups) if rows else 0
    return RankedRows(rows=results, total=total)
