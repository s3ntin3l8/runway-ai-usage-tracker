"""Shared helpers — _parse_ts and _parse_period_key are needed by
more than one query module."""

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def _parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse a timestamp from either a string or datetime object returned by SQLite."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    # SQLite returns ISO strings; may have 'T' or space separator, with/without tz
    s = str(value).replace(" ", "T")
    if not s.endswith("Z") and "+" not in s and len(s) <= 19:
        s += "+00:00"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 14.2  query_cost_forecast
# ---------------------------------------------------------------------------


def _parse_period_key(key: str, period_type: str) -> datetime | None:
    """Parse a period_key into a UTC datetime."""
    try:
        if period_type == "hour":
            return datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=UTC)
        if period_type == "day":
            return datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=UTC)
        if period_type == "month":
            return datetime.strptime(key, "%Y-%m").replace(day=1, tzinfo=UTC)
        if period_type == "year":
            return datetime.strptime(key, "%Y").replace(month=1, day=1, tzinfo=UTC)
    except ValueError:
        logger.debug("Unrecognised period_key format %r for type %r", key, period_type)
    return None
