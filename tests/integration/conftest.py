import pytest

from app.core.rate_limit import limiter

# Phase 1 schema reset: these test files reference deleted DB models
# (UsageSnapshot, UsageSnapshotModel, CumulativeUsage) and will be
# rewritten in Phase 7 (read endpoints) and Phase 8 (cumulative migration).
# Excluded from collection until then.
collect_ignore = [
    "test_csv_export.py",
    "test_cumulative_endpoint.py",
    "test_fleet_endpoint.py",
    "test_history.py",
    "test_fleet_hud_browser.py",
    "test_forecast_endpoint.py",
]


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset slowapi in-memory counters before each integration test.

    All integration tests share the same app singleton, so rate-limit buckets
    accumulate across tests.  Without this reset, endpoints with tight limits
    (e.g. 10/minute) become 429 by the time later tests reach them.
    """
    limiter.reset()
