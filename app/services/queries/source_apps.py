"""Per-client-app usage breakdown over usage_events.

``provider_id`` names the *upstream* provider a request was billed to, and
several client apps share one provider — MiniMax Code, OpenCode and a plain
API script can all spend the same MiniMax plan. A canonical-provider retag
(see ``scripts/sidecar_pkg/canonical_providers.py``) therefore folds several
apps onto one provider id, and the app that actually made the call would be
lost. ``source_app`` keeps that fact, and this query reports it.

Groups the event log by ``source_app`` — the live-events approach
``query_cumulative_live`` uses, so the current-month default stays tz-correct
(the UTC-keyed rollup lags the user-local month boundary).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from app.services.queries._shared import sqlite_utc_timestamp


def query_source_apps(
    session: Session,
    *,
    since: datetime,
    until: datetime | None = None,
    provider_id: str | None = None,
    account_id: str | None = None,
    sidecar_id: str | None = None,
    metric: str = "tokens",
    exclude_cache: bool = False,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Rank ``source_app`` in ``[since, until)`` by tokens or cost.

    Only ``kind == "message"`` rows with a non-NULL ``source_app`` count —
    rows written before the column landed carry the provider id instead (see
    ``_backfill_source_app``), so they still group, just under the provider
    name. Each row carries the token component sums + ``tokens_total``,
    ``cost_usd`` + ``cost_cache`` (cache_read + cache_create), ``msgs``, and
    ``providers`` (the distinct providers that contributed the app's usage).

    ``metric`` ("tokens" | "cost") chooses the sort key; ``exclude_cache``
    drops cache tokens/cost from that sort key only — the raw component fields
    stay populated so the frontend can recompute either view. Capped at
    ``limit`` rows.
    """
    until_clause = "AND ts < :until" if until is not None else ""
    provider_clause = "AND provider_id = :provider_id" if provider_id else ""
    account_clause = "AND account_id = :account_id" if account_id else ""
    sidecar_clause = "AND sidecar_id = :sidecar_id" if sidecar_id else ""

    # Sort expression mirrors the displayed metric so bar order and bar length
    # agree, and honours exclude-cache the same way the frontend does. These
    # are aggregates (GROUP BY source_app) so they must be wrapped in SUM() — a
    # bare column here would order by an arbitrary row's value, not the total.
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
            source_app,
            COUNT(*)                                            AS msgs,
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
            GROUP_CONCAT(DISTINCT provider_id)                  AS providers_csv
        FROM usage_events
        WHERE kind = 'message'
          AND source_app IS NOT NULL
          AND ts >= :since
          {until_clause}
          {provider_clause}
          {account_clause}
          {sidecar_clause}
        GROUP BY source_app
        ORDER BY {order_expr} DESC
        LIMIT :limit
        """
    )

    params: dict[str, Any] = {"since": sqlite_utc_timestamp(since), "limit": limit}
    if until is not None:
        params["until"] = sqlite_utc_timestamp(until)
    if provider_id:
        params["provider_id"] = provider_id
    if account_id:
        params["account_id"] = account_id
    if sidecar_id:
        params["sidecar_id"] = sidecar_id

    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]

    results: list[dict[str, Any]] = []
    for row in rows:
        providers = [p for p in (row.providers_csv or "").split(",") if p]
        results.append(
            {
                "source_app": row.source_app,
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
            }
        )
    return results
