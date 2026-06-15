"""Cross-provider model ranking over usage_events.

The dashboard's per-provider donuts (ModelDonut/CostDonut) split usage by
model *within* one provider+account. This ranks models across *every*
provider/account so the question "what model did I lean on most, regardless
of where I ran it" has a single answer. Groups by ``model_id`` alone,
collapsing provider/account/sidecar — the same live-events approach
``query_cumulative_live`` uses so the current-month default stays tz-correct
(the UTC-keyed rollup lags the user-local month boundary).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session


def query_top_models(
    session: Session,
    *,
    since: datetime,
    until: datetime | None = None,
    metric: str = "tokens",
    exclude_cache: bool = False,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Rank ``model_id`` across all providers/accounts in ``[since, until)``.

    Only ``kind == "message"`` rows with a non-NULL ``model_id`` count. Each
    row carries the token component sums + ``tokens_total``, ``cost_usd`` +
    ``cost_cache`` (cache_read + cache_create), ``msgs``, and ``providers``
    (the distinct providers that contributed the model).

    ``metric`` ("tokens" | "cost") chooses the sort key; ``exclude_cache``
    drops cache tokens/cost from that sort key only — the raw component fields
    stay populated so the frontend can recompute either view. Capped at
    ``limit`` rows.
    """
    until_clause = "AND ts < :until" if until is not None else ""

    # Sort expression mirrors the displayed metric so bar order and bar length
    # agree, and honours exclude-cache the same way the frontend does. These
    # are aggregates (GROUP BY model_id) so they must be wrapped in SUM() — a
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
            SUM(cost_usd)                                       AS cost_usd,
            SUM(cost_cache_read + cost_cache_create)            AS cost_cache,
            GROUP_CONCAT(DISTINCT provider_id)                  AS providers_csv
        FROM usage_events
        WHERE kind = 'message'
          AND model_id IS NOT NULL
          AND ts >= :since
          {until_clause}
        GROUP BY model_id
        ORDER BY {order_expr} DESC
        LIMIT :limit
        """
    )

    params: dict[str, Any] = {"since": since.isoformat(), "limit": limit}
    if until is not None:
        params["until"] = until.isoformat()

    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]

    results: list[dict[str, Any]] = []
    for row in rows:
        providers = [p for p in (row.providers_csv or "").split(",") if p]
        results.append(
            {
                "model_id": row.model_id,
                "msgs": int(row.msgs or 0),
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
