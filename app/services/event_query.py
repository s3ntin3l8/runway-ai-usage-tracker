"""Compatibility shim — the actual code now lives in `app.services.queries`.

Kept so existing imports like `from app.services.event_query import …`
continue to work. New code should import from `app.services.queries`
directly. See that package's docstring for the module layout.
"""

from app.services.queries import (
    query_anomalies,
    query_chart,
    query_cost_forecast,
    query_events,
    query_heatmap,
    query_history_deltas,
    query_history_grouped,
    query_history_raw,
    query_sessions,
    query_snapshots,
    query_window_aggregation,
    query_window_detail,
    query_window_history,
    query_windows,
)

__all__ = [
    "query_anomalies",
    "query_chart",
    "query_cost_forecast",
    "query_events",
    "query_heatmap",
    "query_history_deltas",
    "query_history_grouped",
    "query_history_raw",
    "query_sessions",
    "query_snapshots",
    "query_window_aggregation",
    "query_window_detail",
    "query_window_history",
    "query_windows",
]
