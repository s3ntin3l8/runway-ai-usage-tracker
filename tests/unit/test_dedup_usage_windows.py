"""Tests for the usage_windows dedup migration planning logic.

plan_group_deletions is pure (no DB), so these build in-memory UsageWindow rows
and assert which ones the migration would delete.
"""

from datetime import UTC, datetime, timedelta

from app.models.db import UsageWindow
from scripts.dedup_usage_windows import plan_group_deletions

_WEEK = timedelta(days=7).total_seconds()
_NOW = datetime(2026, 6, 29, 21, 0, 0, tzinfo=UTC)


def _w(
    end: datetime,
    *,
    wid: int,
    msgs: int,
    model_id: str = "",
    sidecar_id: str = "",
    start: datetime | None = None,
) -> UsageWindow:
    return UsageWindow(
        id=wid,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=start if start is not None else end - timedelta(days=7),
        window_end=end,
        model_id=model_id,
        sidecar_id=sidecar_id,
        msgs=msgs,
    )


def test_jittery_closed_week_collapses_to_canonical_rollup():
    """A closed week with sub-second jitter keeps only the max-msgs rollup tuple."""
    base = datetime(2026, 6, 23, 18, 0, 0, tzinfo=UTC)  # a past, closed week
    rows = [
        _w(base.replace(microsecond=100_000), wid=1, msgs=50),
        _w(base.replace(microsecond=500_000), wid=2, msgs=900),  # canonical (max msgs)
        _w(base.replace(second=59, microsecond=900_000), wid=3, msgs=300),
    ]
    deleted = plan_group_deletions(rows, duration_seconds=_WEEK, now=_NOW)
    deleted_ids = {r.id for r in deleted}
    assert deleted_ids == {1, 3}, "only the max-msgs rollup row survives"


def test_in_progress_week_is_fully_deleted():
    """Every row of a not-yet-closed week (future window_end) is spurious."""
    base = datetime(2026, 6, 30, 18, 0, 0, tzinfo=UTC)  # future — current week
    rows = [
        _w(base.replace(microsecond=100_000), wid=1, msgs=500),
        _w(base.replace(microsecond=300_000), wid=2, msgs=1079),
        _w(base.replace(second=59, microsecond=900_000), wid=3, msgs=6892),
    ]
    deleted = plan_group_deletions(rows, duration_seconds=_WEEK, now=_NOW)
    assert {r.id for r in deleted} == {1, 2, 3}


def test_distinct_real_weeks_are_not_merged():
    """Windows a full duration apart stay separate; each clean week is kept."""
    w1 = datetime(2026, 6, 16, 18, 0, 0, tzinfo=UTC)
    w2 = datetime(2026, 6, 23, 18, 0, 0, tzinfo=UTC)
    rows = [
        _w(w1, wid=1, msgs=400),
        _w(w2, wid=2, msgs=900),
    ]
    deleted = plan_group_deletions(rows, duration_seconds=_WEEK, now=_NOW)
    assert deleted == [], "two distinct clean weeks — nothing to delete"


def test_canonical_tuple_keeps_matching_grain_rows():
    """Grain rows (per-model/per-sidecar) sharing the canonical tuple are kept;
    grains on a jittered tuple are dropped so query_window_history stays coherent."""
    base = datetime(2026, 6, 23, 18, 0, 0, tzinfo=UTC)
    canon = base.replace(microsecond=500_000)
    other = base.replace(microsecond=100_000)
    rows = [
        _w(canon, wid=1, msgs=900),  # canonical rollup
        _w(canon, wid=2, msgs=600, model_id="sonnet"),  # grain on canonical tuple -> keep
        _w(canon, wid=3, msgs=300, sidecar_id="dev-01"),  # grain on canonical tuple -> keep
        _w(other, wid=4, msgs=50),  # stale rollup -> delete
        _w(other, wid=5, msgs=40, model_id="sonnet"),  # grain on stale tuple -> delete
    ]
    deleted = plan_group_deletions(rows, duration_seconds=_WEEK, now=_NOW)
    assert {r.id for r in deleted} == {4, 5}
