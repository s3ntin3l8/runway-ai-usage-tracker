"""Auto-extracted from app.services.event_query during the monolith split.
See app/services/queries/__init__.py for the public surface.
"""

import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from app.models.db import UsagePeriodRollup


def query_anomalies(
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
    lookback_days: int = 30,
    z_threshold: float = 2.0,
) -> dict[str, Any]:
    """Detect per-(provider, account, model_id) token spikes vs recent history.

    For each combination, pulls period_type=day rows covering the last
    lookback_days+1 days.  Today's row is the signal; the prior lookback_days
    rows are the historical baseline.  Emits an anomaly when:
      - z = (today_tokens - mean) / stdev > z_threshold
      - today is non-zero
      - historical stdev > 0 and n >= 2.
    """
    now = datetime.now(UTC)
    today_key = now.strftime("%Y-%m-%d")
    oldest_key = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    stmt = select(UsagePeriodRollup).where(
        UsagePeriodRollup.period_type == "day",
        UsagePeriodRollup.sidecar_id == "",  # all-sidecars grain
        UsagePeriodRollup.period_key >= oldest_key,
        UsagePeriodRollup.period_key <= today_key,
    )
    if provider_id:
        stmt = stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsagePeriodRollup.account_id == account_id)
    rows = list(session.exec(stmt).all())

    # Group by (provider_id, account_id, model_id)
    by_group: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        key: tuple[str, str, str] = (r.provider_id, r.account_id, r.model_id)
        group = by_group.setdefault(key, {"today": None, "history": []})
        tokens = (
            r.tokens_input
            + r.tokens_output
            + r.tokens_cache_read
            + r.tokens_cache_create
            + r.tokens_reasoning
        )
        if r.period_key == today_key:
            group["today"] = {"tokens": tokens, "cost_usd": r.cost_usd}
        else:
            group["history"].append(tokens)

    anomalies: list[dict[str, Any]] = []
    for (pid, aid, mid), group in sorted(by_group.items()):
        today_data = group["today"]
        if today_data is None:
            continue
        today_tokens = today_data["tokens"]
        if today_tokens == 0:
            continue

        history = group["history"]
        if len(history) < 2:
            continue

        try:
            mean = statistics.mean(history)
            stdev = statistics.stdev(history)  # sample stdev (n-1)
        except statistics.StatisticsError:
            continue

        if stdev == 0:
            continue  # constant history — no meaningful z-score

        z = (today_tokens - mean) / stdev
        if z > z_threshold:
            anomalies.append(
                {
                    "provider_id": pid,
                    "account_id": aid,
                    "model_id": mid,
                    "today_tokens": today_tokens,
                    "today_cost_usd": today_data["cost_usd"],
                    "historical_mean_tokens": round(mean, 2),
                    "historical_stddev_tokens": round(stdev, 2),
                    "z_score_tokens": round(z, 4),
                    "verdict": "spike",
                }
            )

    return {
        "as_of": now.isoformat(),
        "lookback_days": lookback_days,
        "z_threshold": z_threshold,
        "anomalies": anomalies,
    }


# ---------------------------------------------------------------------------
# 16  History queries (restored from event-sourced model)
# ---------------------------------------------------------------------------
