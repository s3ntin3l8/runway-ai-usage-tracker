"""Live cumulative aggregation over usage_events.

"This period" and "Yearly" totals are *live gauges*: they must reset on the
user's local calendar boundary, not the UTC boundary the pre-aggregated
``usage_period_rollup`` table is keyed on. Computing them on demand from the
authoritative event log (anchored at a local-tz period start) keeps them
correct across timezone changes with no stored-key migration.

Mirrors ``query_window_aggregation`` (windows.py) but groups by identity as
well as model/sidecar and emits the cumulative-bucket shape consumed by the
``/usage/cumulative`` endpoint.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from app.models.db import UsageEvent


def _empty_grain() -> dict[str, Any]:
    """A by_model / by_sidecar leaf bucket."""
    return {
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_cache_read": 0,
        "tokens_cache_create": 0,
        "tokens_reasoning": 0,
        "msgs": 0,
        "cost_usd": 0.0,
        # Cache portion of cost_usd (cache_read + cache_create), for exclude-cache.
        "cost_cache": 0.0,
    }


def _empty_bucket() -> dict[str, Any]:
    """A top-level cumulative bucket (totals + by_model + by_sidecar)."""
    b = _empty_grain()
    b["by_model"] = {}
    b["by_sidecar"] = {}
    return b


def _accumulate(grain: dict[str, Any], delta: dict[str, Any]) -> None:
    for key, value in delta.items():
        grain[key] += value


def query_cumulative_live(
    session: Session,
    *,
    since: datetime,
    until: datetime | None = None,
    provider_id: str | None = None,
    account_id: str | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Aggregate billable usage_events in ``[since, until)`` per identity.

    Returns ``{(provider_id, account_id): bucket}`` where each bucket carries
    token totals, ``cost_usd``, ``msgs``, and ``by_model`` / ``by_sidecar``
    breakdowns — the same shape ``/usage/cumulative`` builds from the rollup.
    Only ``kind == "message"`` rows are counted (errors excluded), matching
    the rollup semantics.
    """
    stmt = select(  # type: ignore[call-overload]
        UsageEvent.provider_id,
        UsageEvent.account_id,
        UsageEvent.model_id,
        UsageEvent.sidecar_id,
        func.count(UsageEvent.id),  # type: ignore[arg-type]
        func.sum(UsageEvent.tokens_input),
        func.sum(UsageEvent.tokens_output),
        func.sum(UsageEvent.tokens_cache_read),
        func.sum(UsageEvent.tokens_cache_create),
        func.sum(UsageEvent.tokens_reasoning),
        func.sum(UsageEvent.cost_usd),
        func.sum(UsageEvent.cost_cache_read),
        func.sum(UsageEvent.cost_cache_create),
    ).where(
        UsageEvent.kind == "message",
        UsageEvent.ts >= since,
    )
    if until is not None:
        stmt = stmt.where(UsageEvent.ts < until)
    if provider_id:
        stmt = stmt.where(UsageEvent.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsageEvent.account_id == account_id)
    stmt = stmt.group_by(
        UsageEvent.provider_id,
        UsageEvent.account_id,
        UsageEvent.model_id,
        UsageEvent.sidecar_id,
    )

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for pid, aid, mid, sid, msgs, ti, to, tcr, tcc, tr, cost, ccr, ccc in session.exec(stmt).all():
        delta = {
            "tokens_input": ti or 0,
            "tokens_output": to or 0,
            "tokens_cache_read": tcr or 0,
            "tokens_cache_create": tcc or 0,
            "tokens_reasoning": tr or 0,
            "msgs": msgs,
            "cost_usd": cost or 0.0,
            "cost_cache": (ccr or 0.0) + (ccc or 0.0),
        }
        bucket = out.setdefault((pid, aid), _empty_bucket())
        _accumulate(bucket, delta)  # top-level total = sum over every (model, sidecar) grain
        if mid:
            _accumulate(bucket["by_model"].setdefault(mid, _empty_grain()), delta)
        if sid:
            _accumulate(bucket["by_sidecar"].setdefault(sid, _empty_grain()), delta)
    return out
