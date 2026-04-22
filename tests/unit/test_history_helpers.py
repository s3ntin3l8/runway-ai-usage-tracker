"""Tests for usage history helper functions."""

from datetime import UTC, datetime, timedelta

import pytest

from app.api.endpoints.usage import (
    _classify_window,
    _dedupe_with_peaks,
    _effective_label,
    _group_snapshots,
    _pick_bucket_seconds,
)
from app.models.db import UsageSnapshot


def _make_snapshot(
    *,
    ts: datetime,
    provider: str = "anthropic",
    account: str = "acc1",
    window_type: str = "weekly",
    used: float = 50.0,
    limit: float = 100.0,
    unit: str = "percent",
    account_label: str | None = None,
) -> UsageSnapshot:
    return UsageSnapshot(
        timestamp=ts,
        provider_id=provider,
        account_id=account,
        service_name="Test",
        window_type=window_type,
        used_value=used,
        limit_value=limit,
        unit_type=unit,
        account_label=account_label,
        health="good",
        remaining="50%",
        reset="in 1 day",
        data_source="api",
        input_source="config",
    )


# ── _effective_label ─────────────────────────────────────────────────────────


def test_effective_label_none():
    assert _effective_label(None) is None


def test_effective_label_empty():
    assert _effective_label("") is None


def test_effective_label_default():
    assert _effective_label("default") is None
    assert _effective_label("Default") is None


def test_effective_label_real():
    assert _effective_label("alice@example.com") == "alice@example.com"


# ── _classify_window ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "window,provider,expected",
    [
        ("session", "anthropic", "session"),
        ("daily", "gemini", "session"),
        ("hourly", "openai", "session"),
        ("prepaid", "anthropic", "session"),
        ("weekly", "anthropic", "weekly"),
        ("monthly", "anthropic", "weekly"),
        ("biweekly", "anthropic", "weekly"),
        # Credit providers: monthly = credit bucket = weekly
        ("monthly", "openrouter", "weekly"),
        ("monthly", "minimax", "weekly"),
        # Credit providers: unknown → other
        ("daily", "openrouter", "session"),
        # Unknown window_type
        ("unknown", "anthropic", "other"),
        (None, "anthropic", "other"),
    ],
)
def test_classify_window(window, provider, expected):
    assert _classify_window(window, provider) == expected


def test_classify_window_model_scoped_weekly_goes_to_other():
    """weekly + model_id set → Additional column (avoids single-cell collision)."""
    assert _classify_window("weekly", "anthropic", "sonnet") == "other"
    assert _classify_window("weekly", "anthropic", "opus") == "other"
    assert _classify_window("weekly", "anthropic", "design") == "other"
    assert _classify_window("weekly", "anthropic", None) == "weekly"


# ── _pick_bucket_seconds ─────────────────────────────────────────────────────


def test_pick_bucket_seconds_7d():
    assert _pick_bucket_seconds(7.0) == 10800  # 3-hourly → ~56 pts


def test_pick_bucket_seconds_30d():
    assert _pick_bucket_seconds(30.0) == 86400  # daily → ~30 pts


def test_pick_bucket_seconds_1d():
    assert _pick_bucket_seconds(1.0) == 1800  # 30-min → ~48 pts


def test_pick_bucket_seconds_6h():
    assert _pick_bucket_seconds(0.25) == 900  # 15-min → ~24 pts


def test_pick_bucket_seconds_1h():
    assert _pick_bucket_seconds(0.05) == 300  # 5-min → ~12 slots


# ── _group_snapshots bucketing regression ────────────────────────────────────


def test_group_snapshots_same_daily_bucket_collapse():
    """Two snapshots 30 minutes apart in a daily bucket produce one row, not two.

    Regression test for the original bug where ts.replace(second=0, microsecond=0)
    only zeroed sub-second fields, leaving hour/minute intact, so snapshots at
    different times of day hashed to different keys.
    """
    base = datetime(2026, 4, 22, 9, 0, 0, tzinfo=UTC)
    thirty_min_later = base + timedelta(minutes=30)

    snaps = [
        _make_snapshot(ts=base, used=40.0),
        _make_snapshot(ts=thirty_min_later, used=50.0),
    ]

    # Daily bucket (86400s)
    rows = _group_snapshots(snaps, bucket_seconds=86400)
    assert len(rows) == 1, f"Expected 1 row, got {len(rows)}: {[r['timestamp'] for r in rows]}"


def test_group_snapshots_different_days_separate_rows():
    """Snapshots on different days produce separate rows."""
    day1 = datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC)
    day2 = datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC)

    snaps = [
        _make_snapshot(ts=day1, used=40.0),
        _make_snapshot(ts=day2, used=50.0),
    ]

    rows = _group_snapshots(snaps, bucket_seconds=86400)
    assert len(rows) == 2


def test_group_snapshots_session_and_weekly_in_same_row():
    """Session + weekly snapshots from same bucket end up in same row."""
    ts = datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC)
    snaps = [
        _make_snapshot(ts=ts, window_type="session", used=20.0),
        _make_snapshot(ts=ts + timedelta(seconds=5), window_type="weekly", used=60.0),
    ]

    rows = _group_snapshots(snaps, bucket_seconds=3600)
    assert len(rows) == 1
    row = rows[0]
    assert row["session"] is not None
    assert row["weekly"] is not None
    assert row["session"]["value"] == 20.0
    assert row["weekly"]["value"] == 60.0


def test_group_snapshots_label_map_overlay():
    """label_map overlays custom labels onto snapshots with NULL account_label."""
    ts = datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC)
    snap = _make_snapshot(ts=ts, account="acc1", account_label=None)

    label_map = {("anthropic", "acc1"): "alice@example.com"}
    rows = _group_snapshots([snap], bucket_seconds=3600, label_map=label_map)

    assert len(rows) == 1
    assert rows[0]["account_label"] == "alice@example.com"


# ── _dedupe_with_peaks ───────────────────────────────────────────────────────


def test_dedupe_with_peaks_empty():
    averages, peaks = _dedupe_with_peaks([], 3600)
    assert averages == []
    assert peaks == []


def test_dedupe_with_peaks_deduplicates():
    """Multiple snapshots in the same bucket produce one average row."""
    ts = datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC)
    snaps = [
        _make_snapshot(ts=ts, used=40.0),
        _make_snapshot(ts=ts + timedelta(seconds=30), used=50.0),
        _make_snapshot(ts=ts + timedelta(seconds=59), used=45.0),
    ]
    averages, peaks = _dedupe_with_peaks(snaps, 3600)
    assert len(averages) == 1


def test_dedupe_with_peaks_tracks_max():
    """Peak row has the highest used_value in the bucket."""
    ts = datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC)
    snaps = [
        _make_snapshot(ts=ts, used=30.0),
        _make_snapshot(ts=ts + timedelta(seconds=30), used=80.0),
        _make_snapshot(ts=ts + timedelta(seconds=59), used=50.0),
    ]
    _, peaks = _dedupe_with_peaks(snaps, 3600)
    assert peaks[0].used_value == 80.0
