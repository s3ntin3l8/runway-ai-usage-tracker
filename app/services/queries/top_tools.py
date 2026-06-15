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


def query_top_tools(
    session: Session,
    *,
    since: datetime,
    until: datetime | None = None,
    provider_id: str | None = None,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Rank tool names by invocation count in ``[since, until)``.

    Each row: ``tool`` (name), ``calls`` (total invocations), ``msgs`` (distinct
    messages that used it). Ordered by calls desc.
    """
    until_clause = "AND e.ts < :until" if until is not None else ""
    provider_clause = "AND e.provider_id = :provider_id" if provider_id else ""

    sql = text(
        f"""
        SELECT
            j.value          AS tool,
            COUNT(*)         AS calls,
            COUNT(DISTINCT e.id) AS msgs
        FROM usage_events e
        JOIN json_each(e.tools_json) j
        WHERE e.kind = 'message'
          AND e.tools_json IS NOT NULL
          AND e.ts >= :since
          {until_clause}
          {provider_clause}
        GROUP BY j.value
        ORDER BY calls DESC
        LIMIT :limit
        """
    )

    params: dict[str, Any] = {"since": since.isoformat(), "limit": limit}
    if until is not None:
        params["until"] = until.isoformat()
    if provider_id:
        params["provider_id"] = provider_id

    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]
    return [
        {"tool": row.tool, "calls": int(row.calls or 0), "msgs": int(row.msgs or 0)}
        for row in rows
        if row.tool
    ]
