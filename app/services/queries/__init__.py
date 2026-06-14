"""Read-side query helpers for the event-sourced usage data model.

Split out of the original 1979-line app.services.event_query monolith
during the audit's R11 cleanup. The public surface is preserved in
full — every name that was importable from `app.services.event_query`
is still importable here.

Modules:
    events    — raw UsageEvent lookups
    windows   — UsageWindow aggregates and per-window snapshots
    heatmap   — 7x24 token heatmap (UTC + local-tz)
    sessions  — per-session aggregations with subagent breakdowns
    forecast  — cost-forecast projections
    anomaly   — z-score anomaly detection
    history   — paginated history views built from events + rollups
    snapshots — quota-snapshot timelines for the History dashboard tab
"""

from app.services.queries.anomaly import query_anomalies
from app.services.queries.cumulative import query_cumulative_live
from app.services.queries.events import count_events, event_time_range, query_events
from app.services.queries.forecast import query_cost_forecast
from app.services.queries.heatmap import query_heatmap
from app.services.queries.history import (
    query_history_deltas,
    query_history_grouped,
    query_history_raw,
)
from app.services.queries.sessions import query_sessions
from app.services.queries.snapshots import (
    query_chart,
    query_snapshots,
    query_window_detail,
    query_windows,
)
from app.services.queries.windows import (
    query_window_aggregation,
    query_window_history,
)

__all__ = [
    "count_events",
    "event_time_range",
    "query_anomalies",
    "query_chart",
    "query_cost_forecast",
    "query_cumulative_live",
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
