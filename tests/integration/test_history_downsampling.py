"""Integration tests for SQL bucketing in `query_snapshots` / `query_chart`.

The bucket-resolution table in `app/services/queries/snapshots.py`:
    days <= 0.1  → 60s buckets
    days <= 1.0  → 300s buckets  (5 min)
    days <= 7.0  → 1800s buckets (30 min)
    days <= 30.0 → 10800s buckets (3 hours)
    else         → 86400s buckets (1 day)
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import LatestUsage, QuotaSnapshot, UsageEvent, UsageWindow
from app.services.pricing_seed import seed_pricing_table
from app.services.queries.snapshots import (
    _bucket_seconds_for,
    query_chart,
    query_snapshots,
)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        seed_pricing_table(s)
        yield s


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _bucket_interior(bucket_seconds: int = 300) -> datetime:
    """A UTC instant safely inside a single downsampling bucket.

    Buckets key on ``floor(epoch / bucket_seconds)``, so samples anchored to
    ``now`` and a few seconds apart land in *adjacent* buckets whenever the test
    runs within those few seconds of a boundary — a real flake we hit in CI
    (``assert 2 == 1``). Anchoring to the midpoint of the previous full bucket
    keeps a small cluster of samples in one bucket and always in the past.
    """
    epoch = int(datetime.now(UTC).timestamp())
    bucket_start = epoch - (epoch % bucket_seconds)
    return datetime.fromtimestamp(bucket_start - bucket_seconds // 2, UTC)


# ---------------------------------------------------------------------------
# Bucket resolution
# ---------------------------------------------------------------------------


def test_bucket_resolution_picks_right_tier():
    assert _bucket_seconds_for(0.05) == 60
    assert _bucket_seconds_for(0.1) == 60
    assert _bucket_seconds_for(0.5) == 300
    assert _bucket_seconds_for(1.0) == 300
    assert _bucket_seconds_for(3.0) == 1800
    assert _bucket_seconds_for(7.0) == 1800
    assert _bucket_seconds_for(15.0) == 10800
    assert _bucket_seconds_for(30.0) == 10800
    assert _bucket_seconds_for(60.0) == 21600
    assert _bucket_seconds_for(90.0) == 21600
    assert _bucket_seconds_for(365.0) == 86400


# ---------------------------------------------------------------------------
# query_snapshots: bucketing
# ---------------------------------------------------------------------------


def test_query_snapshots_buckets_collapse_polls(session):
    """60 snapshots 30s apart over 30 min, days=1 (5-min buckets) ⇒ ≤ 6 rows."""
    now = datetime.now(UTC)
    for i in range(60):
        session.add(
            QuotaSnapshot(
                provider_id="anthropic",
                account_id="user@example.com",
                window_type="weekly",
                model_id="",
                ts=now - timedelta(seconds=30 * i),
                pct_used=10.0 + i * 0.1,
            )
        )
    session.commit()

    result = query_snapshots(session, days=1, limit=500)
    # 30 min of polls / 5-min buckets ⇒ at most 7 buckets (worst-case
    # alignment at boundary). Anything much smaller than 60 proves
    # bucketing happened.
    assert result["total"] <= 7
    assert result["total"] < 60
    assert len(result["rows"]) == result["total"]


def test_query_snapshots_picks_latest_in_bucket(session):
    """Two snapshots in the same 5-min bucket ⇒ latest ts wins."""
    base = _bucket_interior()
    # Both within the same 5-minute bucket (5 sec apart), clear of either edge.
    session.add(
        QuotaSnapshot(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            ts=base,
            pct_used=40.0,
        )
    )
    session.add(
        QuotaSnapshot(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            ts=base + timedelta(seconds=5),
            pct_used=55.0,
        )
    )
    session.commit()

    result = query_snapshots(session, days=1, limit=10)
    assert result["total"] == 1
    assert result["rows"][0]["pct_used"] == 55.0


def test_query_snapshots_delta_across_buckets(session):
    """Three snapshots in three distinct 5-min buckets ⇒ deltas [None, 15, 20]."""
    now = datetime.now(UTC)
    # 5-min buckets at days=1; space rows by 7 minutes so each is its own bucket.
    for i, pct in enumerate([10.0, 25.0, 45.0]):
        session.add(
            QuotaSnapshot(
                provider_id="anthropic",
                account_id="user@example.com",
                window_type="weekly",
                model_id="",
                ts=now - timedelta(minutes=7 * (2 - i)),
                pct_used=pct,
            )
        )
    session.commit()

    result = query_snapshots(session, days=1, limit=10)
    # Newest first: 45, 25, 10
    assert [r["pct_used"] for r in result["rows"]] == [45.0, 25.0, 10.0]
    assert [r["delta"] for r in result["rows"]] == [20.0, 15.0, None]


def test_query_snapshots_single_snapshot_no_delta(session):
    now = datetime.now(UTC)
    session.add(
        QuotaSnapshot(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            ts=now,
            pct_used=33.0,
        )
    )
    session.commit()

    result = query_snapshots(session, days=1, limit=10)
    assert result["total"] == 1
    assert result["rows"][0]["delta"] is None


def test_query_snapshots_sql_pagination_total(session):
    """200 distinct (series, bucket) pairs ⇒ paginates 50 per page in SQL."""
    now = datetime.now(UTC)
    # 200 distinct model_ids, one snapshot each: each is its own (series, bucket).
    # pct_used starts at 1 — zero-pct buckets are filtered out of the page
    # query, and we want to count all 200 rows here.
    for i in range(200):
        session.add(
            QuotaSnapshot(
                provider_id="anthropic",
                account_id="user@example.com",
                window_type="weekly",
                model_id=f"m-{i:03d}",
                ts=now - timedelta(minutes=i),
                pct_used=float(i + 1),
            )
        )
    session.commit()

    page1 = query_snapshots(session, days=1, page=1, limit=50)
    page2 = query_snapshots(session, days=1, page=2, limit=50)
    assert page1["total"] == 200
    assert len(page1["rows"]) == 50
    assert page2["total"] == 200
    assert len(page2["rows"]) == 50

    # No overlap between pages.
    page1_ts = {r["ts"] for r in page1["rows"]}
    page2_ts = {r["ts"] for r in page2["rows"]}
    assert page1_ts.isdisjoint(page2_ts)


def test_query_snapshots_window_type_filter(session):
    now = datetime.now(UTC)
    for wt in ("weekly", "daily"):
        session.add(
            QuotaSnapshot(
                provider_id="anthropic",
                account_id="user@example.com",
                window_type=wt,
                model_id="",
                ts=now,
                pct_used=20.0,
            )
        )
    session.commit()

    weekly = query_snapshots(session, days=1, window_type="weekly", limit=10)
    daily = query_snapshots(session, days=1, window_type="daily", limit=10)
    all_ = query_snapshots(session, days=1, window_type="all", limit=10)

    assert weekly["total"] == 1
    assert weekly["rows"][0]["window_type"] == "weekly"
    assert daily["total"] == 1
    assert daily["rows"][0]["window_type"] == "daily"
    assert all_["total"] == 2


def test_query_snapshots_provider_ids_filter(session):
    """`provider_ids` accepts a list and filters via SQL IN, so the row count
    matches the visible rows even with multiple providers selected. Regression
    for sparse pages when the table filtered client-side after server pagination."""
    now = datetime.now(UTC)
    for pid in ("anthropic", "openai", "google"):
        session.add(
            QuotaSnapshot(
                provider_id=pid,
                account_id="user@example.com",
                window_type="weekly",
                model_id="",
                ts=now,
                pct_used=20.0,
            )
        )
    session.commit()

    none_filter = query_snapshots(session, days=1, limit=10)
    single = query_snapshots(session, days=1, provider_ids=["anthropic"], limit=10)
    pair = query_snapshots(session, days=1, provider_ids=["anthropic", "openai"], limit=10)

    assert none_filter["total"] == 3
    assert single["total"] == 1
    assert {r["provider_id"] for r in single["rows"]} == {"anthropic"}
    assert pair["total"] == 2
    assert {r["provider_id"] for r in pair["rows"]} == {"anthropic", "openai"}


def test_query_snapshots_hides_zero_pct_from_count_and_rows(session):
    """Zero-pct buckets are hidden in both the page rows and the total count,
    so pagination stays accurate (no empty late pages). Regression for the
    case where server returned 200-row totals but later pages were all zeros
    and the client filtered them, leaving "no data" tables mid-pagination."""
    now = datetime.now(UTC)
    # 5 distinct series, each in its own (series, bucket): 3 with non-zero
    # pct_used, 2 with pct_used=0. Only the 3 non-zero should be counted.
    for i, pct in enumerate([0.0, 10.0, 0.0, 25.0, 40.0]):
        session.add(
            QuotaSnapshot(
                provider_id="anthropic",
                account_id="user@example.com",
                window_type="weekly",
                model_id=f"m-{i}",
                ts=now - timedelta(minutes=i),
                pct_used=pct,
            )
        )
    session.commit()

    result = query_snapshots(session, days=1, limit=50)
    assert result["total"] == 3
    assert len(result["rows"]) == 3
    assert sorted(r["pct_used"] for r in result["rows"]) == [10.0, 25.0, 40.0]


def test_query_snapshots_preserves_null_pct_used(session):
    """Null-pct snapshots still appear in the table response (they are shown
    as 'unknown %' rows). Only the chart query filters nulls."""
    now = datetime.now(UTC)
    session.add(
        QuotaSnapshot(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            ts=now,
            pct_used=None,
        )
    )
    session.commit()

    result = query_snapshots(session, days=1, limit=10)
    assert result["total"] == 1
    assert result["rows"][0]["pct_used"] is None


# ---------------------------------------------------------------------------
# query_snapshots: enrichment join
# ---------------------------------------------------------------------------


def test_query_snapshots_window_stats_join_uses_reset_at_minute_bucket(session):
    """Sub-minute jitter between snapshot.reset_at and usage_windows.window_end
    still matches via the minute-bucket join key."""
    now = datetime.now(UTC)
    reset_naive = _naive(now + timedelta(hours=1))  # past for the snapshot
    # Pin reset_at / window_end to :30 of a recent *past* minute so the 800ms jitter
    # between them can't straddle a minute boundary into different buckets — the test
    # otherwise flakes when run near the top of a minute. The snapshot ts stays at
    # real `now` (never future) so the days=1 window still includes it.
    reset_anchor = (now - timedelta(minutes=1)).replace(second=30, microsecond=0)
    snap_reset = _naive(reset_anchor)
    # window_end differs by 800ms (sub-minute jitter) but stays in the same minute.
    window_end = _naive(reset_anchor - timedelta(microseconds=800_000))

    session.add(
        QuotaSnapshot(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            ts=_naive(now),
            pct_used=80.0,
            reset_at=snap_reset,
        )
    )
    session.add(
        UsageWindow(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            sidecar_id="",
            window_start=_naive(now - timedelta(days=7)),
            window_end=window_end,
            tokens_input=1000,
            tokens_output=500,
            tokens_cache_read=0,
            tokens_cache_create=0,
            tokens_reasoning=0,
            cost_usd=2.50,
        )
    )
    session.commit()
    _ = reset_naive  # unused but keeps the variable name explanatory above

    result = query_snapshots(session, days=1, limit=10)
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["tokens_total"] == 1500
    assert row["cost_usd"] == 2.50


def test_query_snapshots_open_window_event_sum_correct_across_pages(session):
    """tokens_total enrichment must be correct on page 1 AND page 2 — the
    previous Python implementation computed it once over all snapshots; the
    new code computes per-page, so pages must each see correct sums."""
    now = datetime.now(UTC)
    future_reset = _naive(now + timedelta(hours=1))

    # 75 distinct accounts, each with one open-window snapshot and one event.
    for i in range(75):
        aid = f"u{i:03d}@example.com"
        session.add(
            QuotaSnapshot(
                provider_id="anthropic",
                account_id=aid,
                window_type="weekly",
                model_id="",
                ts=_naive(now - timedelta(seconds=i)),
                pct_used=10.0,
                reset_at=future_reset,
            )
        )
        session.add(
            UsageEvent(
                event_id=f"e{i}",
                provider_id="anthropic",
                account_id=aid,
                sidecar_id="local",
                model_id="claude-sonnet",
                ts=_naive(now - timedelta(minutes=30)),
                kind="message",
                tokens_input=100 * (i + 1),
                tokens_output=0,
                tokens_cache_read=0,
                tokens_cache_create=0,
                tokens_reasoning=0,
                cost_usd=0.01,
            )
        )
    session.commit()

    page1 = query_snapshots(session, days=1, page=1, limit=50)
    page2 = query_snapshots(session, days=1, page=2, limit=50)

    # Both pages must report the per-account token totals (not None, not zero).
    p1_tokens = [r["tokens_total"] for r in page1["rows"]]
    p2_tokens = [r["tokens_total"] for r in page2["rows"]]
    assert all(t is not None and t > 0 for t in p1_tokens), p1_tokens
    assert all(t is not None and t > 0 for t in p2_tokens), p2_tokens


# ---------------------------------------------------------------------------
# query_chart percent path
# ---------------------------------------------------------------------------


def test_query_chart_percent_filters_null_pct_used(session):
    """A null-pct snapshot must never be the bucket representative."""
    base = _bucket_interior()
    # Both within the same 5-min bucket at days=1; the null sample is the later one.
    session.add(
        QuotaSnapshot(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            ts=base,
            pct_used=42.0,
        )
    )
    session.add(
        QuotaSnapshot(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            ts=base + timedelta(seconds=5),
            pct_used=None,
        )
    )
    session.commit()

    result = query_chart(session, metric="percent", days=1)
    assert len(result["series"]) == 1
    points = result["series"][0]["points"]
    assert len(points) == 1
    assert points[0]["pct_used"] == 42.0


def test_query_chart_percent_seed_from_latest_usage_still_fires(session):
    """No QuotaSnapshot rows; one LatestUsage card ⇒ chart seeds one point."""
    session.add(
        LatestUsage(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            variant="default",
            card_json='{"pct_used": 30.0, "limit_value": 100.0}',
        )
    )
    session.commit()

    result = query_chart(session, metric="percent", days=1)
    assert len(result["series"]) == 1
    s = result["series"][0]
    assert s["provider_id"] == "anthropic"
    assert s["window_type"] == "weekly"
    assert len(s["points"]) == 1
    assert s["points"][0]["pct_used"] == 30.0


def test_query_chart_percent_seed_does_not_duplicate_when_snapshots_exist(session):
    """If snapshots exist for a series, the LatestUsage seed must not add a
    duplicate point."""
    now = datetime.now(UTC)
    for i, pct in enumerate([10.0, 25.0]):
        session.add(
            QuotaSnapshot(
                provider_id="anthropic",
                account_id="user@example.com",
                window_type="weekly",
                model_id="",
                ts=now - timedelta(minutes=10 * (1 - i)),
                pct_used=pct,
            )
        )
    session.add(
        LatestUsage(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            variant="default",
            card_json='{"pct_used": 99.0, "limit_value": 100.0}',
        )
    )
    session.commit()

    result = query_chart(session, metric="percent", days=1)
    assert len(result["series"]) == 1
    pts = result["series"][0]["points"]
    # Two snapshot points; no seeded 99.0 row.
    assert len(pts) == 2
    assert {p["pct_used"] for p in pts} == {10.0, 25.0}
