"""Tests for usage history helper functions."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.api.endpoints.usage import (
    _build_by_model_lookup,
    _classify_window,
    _effective_label,
    _group_snapshots,
    _pick_bucket_seconds,
)
from app.models.db import UsageSnapshot, UsageSnapshotModel


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


def test_group_snapshots_additional_preserves_token_usage():
    """Additional entries must carry token_usage and msgs, not just value/unit."""
    ts = datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC)
    snap = UsageSnapshot(
        timestamp=ts,
        provider_id="anthropic",
        account_id="acc1",
        service_name="Claude",
        window_type="seven_day_sonnet",
        used_value=38.0,
        limit_value=100.0,
        unit_type="percent",
        account_label=None,
        health="good",
        remaining="62%",
        reset="in 7 days",
        data_source="api",
        input_source="config",
        tokens_input=1200.0,
        tokens_output=800.0,
        tokens_total=2000.0,
        msgs=42,
    )
    rows = _group_snapshots([snap], bucket_seconds=3600)
    assert len(rows) == 1
    additional = rows[0]["additional"]
    assert additional is not None and len(additional) == 1
    assert additional[0]["token_usage"] == {
        "input": 1200.0,
        "output": 800.0,
        "reasoning": None,
        "cache_read": None,
        "total": 2000.0,
    }
    assert additional[0]["msgs"] == 42


# ── _build_by_model_lookup ───────────────────────────────────────────────────


@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        yield session

    os.close(fd)
    if os.path.exists(db_path):
        os.remove(db_path)


def test_build_by_model_lookup_aggregates_per_bucket(session: Session):
    """_build_by_model_lookup aggregates UsageSnapshotModel by time bucket."""
    now = datetime(2026, 4, 22, 10, 30, 0, tzinfo=UTC)
    bucket_seconds = 1800  # 30-min bucket

    # Create two snapshots in the same bucket
    snap1 = UsageSnapshot(
        provider_id="gemini",
        account_id="user1",
        service_name="Gemini Advanced",
        health="good",
        data_source="api",
        timestamp=now,
        used_value=50.0,
        limit_value=100.0,
        unit_type="percent",
        window_type="monthly",
    )
    snap2 = UsageSnapshot(
        provider_id="gemini",
        account_id="user1",
        service_name="Gemini Advanced",
        health="good",
        data_source="api",
        timestamp=now + timedelta(minutes=5),
        used_value=60.0,
        limit_value=100.0,
        unit_type="percent",
        window_type="monthly",
    )
    session.add(snap1)
    session.add(snap2)
    session.commit()
    session.refresh(snap1)
    session.refresh(snap2)

    # Add model records for both snapshots
    session.add(
        UsageSnapshotModel(
            snapshot_id=snap1.id,
            model_id="flash",
            cost=0.30,
            msgs=3,
            tokens_input=1200.0,
            tokens_output=800.0,
            tokens_total=2000.0,
        )
    )
    session.add(
        UsageSnapshotModel(
            snapshot_id=snap2.id,
            model_id="flash",
            cost=0.20,
            msgs=2,
            tokens_input=800.0,
            tokens_output=500.0,
            tokens_total=1300.0,
        )
    )
    session.add(
        UsageSnapshotModel(
            snapshot_id=snap1.id,
            model_id="pro",
            cost=0.15,
            msgs=1,
            tokens_input=500.0,
            tokens_output=300.0,
            tokens_total=800.0,
        )
    )
    session.commit()

    since = now - timedelta(hours=1)
    lookup = _build_by_model_lookup(session, since, bucket_seconds)

    # Both snapshots are in the same 30-min bucket
    bucket_epoch = int(now.timestamp()) // bucket_seconds * bucket_seconds
    key = (bucket_epoch, "gemini", "user1")

    assert key in lookup
    assert len(lookup[key]) == 2

    by_model = {m["model_id"]: m for m in lookup[key]}
    assert by_model["flash"]["cost"] == 0.50  # 0.30 + 0.20
    assert by_model["flash"]["msgs"] == 5  # 3 + 2
    assert by_model["flash"]["tokens_total"] == 3300.0  # 2000 + 1300
    assert by_model["pro"]["cost"] == 0.15
    assert by_model["pro"]["msgs"] == 1
    assert by_model["pro"]["tokens_total"] == 800.0


def test_build_by_model_lookup_filters_by_provider_and_account(session: Session):
    """_build_by_model_lookup respects provider_id and account_id filters."""
    now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC)
    bucket_seconds = 3600

    snap1 = UsageSnapshot(
        provider_id="gemini",
        account_id="user1",
        service_name="Gemini Advanced",
        health="good",
        data_source="api",
        timestamp=now,
        used_value=50.0,
        limit_value=100.0,
        unit_type="percent",
        window_type="monthly",
    )
    snap2 = UsageSnapshot(
        provider_id="openai",
        account_id="user2",
        service_name="ChatGPT Plus",
        health="good",
        data_source="api",
        timestamp=now,
        used_value=30.0,
        limit_value=100.0,
        unit_type="percent",
        window_type="monthly",
    )
    session.add(snap1)
    session.add(snap2)
    session.commit()
    session.refresh(snap1)
    session.refresh(snap2)

    session.add(
        UsageSnapshotModel(
            snapshot_id=snap1.id,
            model_id="flash",
            cost=0.30,
            msgs=3,
            tokens_total=2000.0,
        )
    )
    session.add(
        UsageSnapshotModel(
            snapshot_id=snap2.id,
            model_id="gpt-4",
            cost=0.50,
            msgs=5,
            tokens_total=3000.0,
        )
    )
    session.commit()

    since = now - timedelta(hours=1)

    # Filter by provider
    lookup = _build_by_model_lookup(session, since, bucket_seconds, provider_id="gemini")
    assert len(lookup) == 1
    key = (int(now.timestamp()) // bucket_seconds * bucket_seconds, "gemini", "user1")
    assert key in lookup
    assert lookup[key][0]["model_id"] == "flash"

    # Filter by account
    lookup = _build_by_model_lookup(session, since, bucket_seconds, account_id="user2")
    assert len(lookup) == 1
    key = (int(now.timestamp()) // bucket_seconds * bucket_seconds, "openai", "user2")
    assert key in lookup
    assert lookup[key][0]["model_id"] == "gpt-4"


def test_build_by_model_lookup_empty(session: Session):
    """_build_by_model_lookup returns empty dict when no data."""
    now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC)
    since = now - timedelta(hours=1)
    lookup = _build_by_model_lookup(session, since, 3600)
    assert lookup == {}
