"""Auto-extracted from app.services.event_query during the monolith split.
See app/services/queries/__init__.py for the public surface.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from app.models._datetime import iso_utc
from app.services.queries._shared import _parse_ts, sqlite_utc_timestamp
from app.services.session_names import resolve_names

# ---------------------------------------------------------------------------
# 7.4  query_sessions
# ---------------------------------------------------------------------------

# Whitelist mapping `sort_by` -> the ORDER BY column/expression. Fixed strings
# only (no interpolation of user input), so safe to splice into the SQL; the
# endpoint further constrains the accepted values via a Literal. `msgs`,
# `tokens_total`, `cost_usd`, and `ts_end` are SELECT aliases below; duration is
# not a stored column (it's derived in Python), so it sorts on the equivalent
# julianday expression, which is monotonic with the rendered duration.
_SORT_COLUMNS = {
    "recent": "ts_end",
    "tokens": "tokens_total",
    "duration": "(julianday(MAX(ts)) - julianday(MIN(ts)))",
    "messages": "msgs",
    "cost": "cost_usd",
}


# Documented `project` sentinel: select events that carry no project label
# (NULL or empty). Emitted as a fixed clause, never as a bound parameter.
NO_PROJECT = "__none__"


@dataclass(frozen=True)
class SessionFilters:
    """The event-level filter set shared by every sessions query.

    One builder feeds the per-session aggregate, the total count and both
    per-session breakdowns, so the page rows, the total and the breakdowns
    always describe the same events. A later cache keys on `cache_key()`.
    """

    provider_id: str
    account_id: str
    since: datetime
    until: datetime | None = None
    project: str | None = None
    sidecar_id: str | None = None
    model: str | None = None
    branch: str | None = None
    exclude_subagents: bool = False

    def where(self) -> str:
        """The WHERE body (no leading keyword). Fixed fragments only; every
        request value is a bound parameter (see `params`)."""
        parts = [
            "provider_id = :provider_id",
            "account_id  = :account_id",
            "session_id IS NOT NULL",
            "ts >= :since",
        ]
        if self.until is not None:
            parts.append("ts < :until")
        if self.project == NO_PROJECT:
            parts.append("(project IS NULL OR project = '')")
        elif self.project is not None:
            parts.append("project = :project")
        if self.sidecar_id:
            parts.append("sidecar_id = :sidecar_id")
        if self.model:
            parts.append("model_id = :model")
        if self.branch:
            parts.append("git_branch = :branch")
        if self.exclude_subagents:
            parts.append("subagent_type IS NULL")
        return "\n          AND ".join(parts)

    def params(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "since": sqlite_utc_timestamp(self.since),
        }
        if self.until is not None:
            out["until"] = sqlite_utc_timestamp(self.until)
        if self.project is not None and self.project != NO_PROJECT:
            out["project"] = self.project
        if self.sidecar_id:
            out["sidecar_id"] = self.sidecar_id
        if self.model:
            out["model"] = self.model
        if self.branch:
            out["branch"] = self.branch
        return out

    def cache_key(self) -> tuple[Any, ...]:
        return (
            self.provider_id,
            self.account_id,
            self.since,
            self.until,
            self.project,
            self.sidecar_id,
            self.model,
            self.branch,
            self.exclude_subagents,
        )


def query_sessions(  # noqa: PLR0913 — the event-level filters extend the signature
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "tokens",
    sort_dir: str = "desc",
    project: str | None = None,
    sidecar_id: str | None = None,
    model: str | None = None,
    branch: str | None = None,
    exclude_subagents: bool = False,
) -> list[dict[str, Any]]:
    """Return top-N sessions by total tokens, newest first within the window.

    Each row includes ts_start, ts_end, duration_seconds, msgs, models[],
    by_model[], tokens_total, tokens_input, tokens_output, tokens_cache,
    cache_hit_pct, cost_usd, sidecar_id, project, cwd, git_branch.

    by_model is a list of per-model aggregates ordered by tokens_total DESC,
    covering all events with a non-NULL model_id in the session.

    `project` filters to sessions whose events carry that project label.
    `model`, `branch` and `exclude_subagents` are event-level like `project`:
    totals, breakdowns and the count all describe only the matching events.
    `project=NO_PROJECT` selects events without a project label.
    `offset` pages the result (for the paginated Sessions browser).

    Events with NULL session_id are excluded.
    """
    if since is None:
        since = datetime.now(UTC) - timedelta(days=7)

    # Whitelisted column + validated direction, with a stable tiebreaker so
    # paging stays deterministic when the sort key ties.
    col = _SORT_COLUMNS.get(sort_by, "tokens_total")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    order_clause = f"{col} {direction}, ts_end DESC, session_id"
    # Exclusive upper bound — only emitted when an `until` is supplied so the
    # default (open-ended) window is byte-identical to the original query.
    filters = SessionFilters(
        provider_id=provider_id,
        account_id=account_id,
        since=since,
        until=until,
        project=project,
        sidecar_id=sidecar_id,
        model=model,
        branch=branch,
        exclude_subagents=exclude_subagents,
    )
    where = filters.where()

    # These SQL f-strings interpolate only server-built clauses from fixed
    # conditions above; all request values stay in bound parameters below.
    #
    # The message count counts *turns*, not events. A provider that emits one
    # event per API request (MiniMax Code) carries its own turn id, so its
    # turns are counted distinctly; a provider that emits one event per message
    # (Claude Code) carries none, and the fallback counts the events — which
    # is the same number, because `event_id` is unique per provider. The
    # Agents card below counts turns the same way, so a session row and its
    # subagent cards speak the same unit.
    # Main aggregation query
    agg_sql = text(
        f"""
        SELECT
            session_id,
            MIN(ts)                                            AS ts_start,
            MAX(ts)                                            AS ts_end,
            COUNT(DISTINCT COALESCE(NULLIF(turn_id, ''), event_id)) AS msgs,
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
            COUNT(DISTINCT CASE WHEN subagent_type IS NOT NULL
                THEN COALESCE(NULLIF(turn_id, ''), event_id) END) AS subagent_msgs
        FROM usage_events
        WHERE {where}
        GROUP BY session_id
        ORDER BY {order_clause}
        LIMIT :limit OFFSET :offset
        """
    )

    # The subagent split counts *turns*, not events. A provider that emits one
    # event per request (MiniMax Code) carries its own turn id, so the turns
    # are counted distinctly; a provider that emits one event per message
    # (Claude Code) carries none, and the fallback keeps the old event count —
    # `event_id` is unique per provider, so the two agree there.
    subagent_sql = text(
        f"""
        SELECT
            subagent_type,
            COUNT(DISTINCT COALESCE(NULLIF(turn_id, ''), event_id)) AS turns,
            COUNT(*)                                            AS requests,
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
        WHERE {where}
          AND session_id  = :session_id
          AND subagent_type IS NOT NULL
        GROUP BY subagent_type
        ORDER BY turns DESC
        """
    )

    model_breakdown_sql = text(
        f"""
        SELECT
            model_id,
            COUNT(DISTINCT COALESCE(NULLIF(turn_id, ''), event_id)) AS msgs,
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
        WHERE {where}
          AND session_id  = :session_id
          AND model_id IS NOT NULL
        GROUP BY model_id
        ORDER BY tokens_total DESC
        """
    )

    rows = session.exec(  # type: ignore[call-overload]
        agg_sql,
        params={**filters.params(), "limit": limit, "offset": offset},
    ).all()

    # One extra query for this page's ids only; the aggregate is untouched.
    names = resolve_names(session, provider_id, [row.session_id for row in rows])

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
        # `subagents=exclude` already removed those events from the aggregate
        # (subagent_msgs is 0), so the block is skipped without a query.
        if subagent_msgs > 0:
            sa_rows = session.exec(  # type: ignore[call-overload]
                subagent_sql,
                params={**filters.params(), "session_id": row.session_id},
            ).all()
            subagents = [
                {
                    "subagent_type": sa.subagent_type,
                    "turns": int(sa.turns or 0),
                    # The event count behind those turns. Equal to `turns` for
                    # a provider that emits one event per message, larger for
                    # one that emits an event per request.
                    "requests": int(sa.requests or 0),
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
                params={**filters.params(), "session_id": row.session_id},
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
                "name": names.get(row.session_id),
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


def count_sessions(  # noqa: PLR0913 — the event-level filters extend the signature
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    project: str | None = None,
    sidecar_id: str | None = None,
    model: str | None = None,
    branch: str | None = None,
    exclude_subagents: bool = False,
) -> int:
    """COUNT(DISTINCT session_id) for the same window/filters as query_sessions."""
    if since is None:
        since = datetime.now(UTC) - timedelta(days=7)
    filters = SessionFilters(
        provider_id=provider_id,
        account_id=account_id,
        since=since,
        until=until,
        project=project,
        sidecar_id=sidecar_id,
        model=model,
        branch=branch,
        exclude_subagents=exclude_subagents,
    )
    sql = text(
        f"""
        SELECT COUNT(DISTINCT session_id) AS n
        FROM usage_events
        WHERE {filters.where()}
        """
    )
    row = session.exec(sql, params=filters.params()).first()  # type: ignore[call-overload]
    return int(row.n or 0) if row else 0


def query_sessions_paginated(  # noqa: PLR0913 — the event-level filters extend the signature
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 0,
    page_size: int = 25,
    sort_by: str = "recent",
    sort_dir: str = "desc",
    project: str | None = None,
    sidecar_id: str | None = None,
    model: str | None = None,
    branch: str | None = None,
    exclude_subagents: bool = False,
) -> dict[str, Any]:
    """One page of sessions + the total count, for the Sessions browser tab."""
    total = count_sessions(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since,
        until=until,
        project=project,
        sidecar_id=sidecar_id,
        model=model,
        branch=branch,
        exclude_subagents=exclude_subagents,
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
        sort_dir=sort_dir,
        project=project,
        sidecar_id=sidecar_id,
        model=model,
        branch=branch,
        exclude_subagents=exclude_subagents,
    )
    return {"sessions": sessions, "total": total, "limit": page_size, "offset": page * page_size}


_OPTION_COLUMNS = {"model": "model_id", "branch": "git_branch"}


def query_session_filter_options(  # noqa: PLR0913 — mirrors the sessions window filters
    session: Session,
    *,
    kind: str,
    provider_id: str,
    account_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    sidecar_id: str | None = None,
    project: str | None = None,
    search: str | None = None,
    limit: int = 50,
) -> list[str]:
    """Top-`limit` distinct models or branches for the sessions filter lists.

    Ranked by event count within the same account/window the sessions list
    uses (and under the active `project`, so the list never offers a value that
    would empty the table); NULL and empty values are excluded. `search` is a case-insensitive
    substring match so a capped list stays reachable by typing.
    """
    column = _OPTION_COLUMNS[kind]
    if since is None:
        since = datetime.now(UTC) - timedelta(days=7)
    filters = SessionFilters(
        provider_id=provider_id,
        account_id=account_id,
        since=since,
        until=until,
        project=project,
        sidecar_id=sidecar_id,
    )
    params = {**filters.params(), "limit": limit}
    needle = ""
    if search:
        escaped = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        needle = f"AND LOWER({column}) LIKE :needle ESCAPE '\\'"
        params["needle"] = f"%{escaped}%"
    sql = text(
        f"""
        SELECT {column} AS value
        FROM usage_events
        WHERE {filters.where()}
          AND {column} IS NOT NULL
          AND {column} != ''
          {needle}
        GROUP BY {column}
        ORDER BY COUNT(*) DESC, {column}
        LIMIT :limit
        """
    )
    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]
    return [r.value for r in rows]
