"""Regression tests for query_chart's percent metric bucketing.

These pin down the peak-per-bucket contract: a series whose intra-bucket peak
is non-zero must surface that peak in the chart, even if the series' value
returns to 0 by the end of the bucket (the classic Gemini-Pro-resets-at-21:08
scenario).
"""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import QuotaSnapshot, UsagePeriodRollup
from app.services.queries.snapshots import query_chart


@pytest.fixture
def db_session():
    fd, db_path = tempfile.mkstemp()
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    os.close(fd)
    if os.path.exists(db_path):
        os.remove(db_path)


_NOW = datetime.now(UTC).replace(microsecond=0)


def _add_snap(
    session: Session,
    *,
    provider_id: str = "gemini",
    model_id: str = "pro",
    window_type: str = "daily",
    ts: datetime,
    pct_used: float,
) -> None:
    session.add(
        QuotaSnapshot(
            provider_id=provider_id,
            account_id="acc1",
            window_type=window_type,
            variant="",
            model_id=model_id,
            ts=ts,
            pct_used=pct_used,
        )
    )
    session.commit()


def _pro_series(result: dict) -> dict | None:
    for s in result.get("series", []):
        if s["provider_id"] == "gemini" and s["model_id"] == "pro":
            return s
    return None


class TestPeakSurvivesEndOfBucketReset:
    """The Gemini-Pro May-13 scenario: peak hits mid-day, post-reset is 0."""

    def test_daily_bucket_returns_peak_not_end_of_day(self, db_session):
        # 90-day window → 1-day buckets (86400s). Place a peak well inside the
        # UTC day and a 0 sample near the end of the same day.
        day_start = (_NOW - timedelta(days=2)).replace(hour=0, minute=0, second=0)
        _add_snap(db_session, ts=day_start + timedelta(hours=12), pct_used=46.0)
        _add_snap(db_session, ts=day_start + timedelta(hours=23, minutes=55), pct_used=0.0)

        result = query_chart(db_session, days=90.0, metric="percent")
        pro = _pro_series(result)
        assert pro is not None, "pro series should exist"
        assert len(pro["points"]) == 1
        assert pro["points"][0]["pct_used"] == 46.0

    def test_all_zero_series_still_returns_zero_point(self, db_session):
        # A series that genuinely was 0 throughout the bucket should still come
        # back (with 0) — frontend filtering is what hides flat-zero series, not
        # this query.
        day_start = (_NOW - timedelta(days=2)).replace(hour=0, minute=0, second=0)
        _add_snap(db_session, ts=day_start + timedelta(hours=10), pct_used=0.0)
        _add_snap(db_session, ts=day_start + timedelta(hours=22), pct_used=0.0)

        result = query_chart(db_session, days=90.0, metric="percent")
        pro = _pro_series(result)
        assert pro is not None
        assert len(pro["points"]) == 1
        assert pro["points"][0]["pct_used"] == 0.0

    def test_multiple_days_each_bucket_independently_picks_peak(self, db_session):
        # Two adjacent UTC days: day1 peaks at 46 then drops to 0; day2 peaks
        # at 20 then drops to 0. Both peaks should surface.
        day1 = (_NOW - timedelta(days=3)).replace(hour=0, minute=0, second=0)
        day2 = day1 + timedelta(days=1)
        _add_snap(db_session, ts=day1 + timedelta(hours=12), pct_used=46.0)
        _add_snap(db_session, ts=day1 + timedelta(hours=23), pct_used=0.0)
        _add_snap(db_session, ts=day2 + timedelta(hours=8), pct_used=20.0)
        _add_snap(db_session, ts=day2 + timedelta(hours=23), pct_used=0.0)

        result = query_chart(db_session, days=90.0, metric="percent")
        pro = _pro_series(result)
        assert pro is not None
        pcts = sorted(p["pct_used"] for p in pro["points"])
        assert pcts == [20.0, 46.0]


def _add_day_rollup(
    session: Session,
    *,
    provider_id: str = "anthropic",
    account_id: str = "acc1",
    day: str,
    tokens_input: int,
) -> None:
    session.add(
        UsagePeriodRollup(
            provider_id=provider_id,
            account_id=account_id,
            period_type="day",
            period_key=day,
            model_id="",
            sidecar_id="",
            tokens_input=tokens_input,
            last_updated=_NOW,
        )
    )
    session.commit()


class TestChartSinceUntilScoping:
    """An explicit since/until pair scopes token bars to a closed period and
    forces daily granularity."""

    def test_tokens_bars_bounded_by_since_until(self, db_session):
        _add_day_rollup(db_session, day="2026-03-31", tokens_input=11)  # before
        _add_day_rollup(db_session, day="2026-04-05", tokens_input=100)  # in
        _add_day_rollup(db_session, day="2026-04-20", tokens_input=50)  # in
        _add_day_rollup(db_session, day="2026-05-01", tokens_input=77)  # at/after until

        result = query_chart(
            db_session,
            metric="tokens",
            since=datetime(2026, 4, 1, tzinfo=UTC),
            until=datetime(2026, 5, 1, tzinfo=UTC),
        )
        dates = sorted(b["date"] for b in result["bars"])
        assert dates == ["2026-04-05", "2026-04-20"]


class TestCostBarsCarryCacheCost:
    """Cost bars expose value_cache = the cache portion of cost (USD), so the
    client can subtract it under the exclude-cache toggle."""

    def test_cost_segment_value_cache_is_cache_cost(self, db_session):
        db_session.add(
            UsagePeriodRollup(
                provider_id="anthropic",
                account_id="acc1",
                period_type="day",
                period_key="2026-04-05",
                model_id="",
                sidecar_id="",
                cost_usd=10.0,
                cost_cache_read=2.0,
                cost_cache_create=1.5,
                last_updated=_NOW,
            )
        )
        db_session.commit()
        result = query_chart(
            db_session,
            metric="cost",
            since=datetime(2026, 4, 1, tzinfo=UTC),
            until=datetime(2026, 5, 1, tzinfo=UTC),
        )
        seg = result["bars"][0]["segments"][0]
        assert seg["value"] == 10.0
        assert seg["value_cache"] == 3.5  # cache_read + cache_create cost


class TestNullsAreIgnored:
    def test_null_pct_used_does_not_become_zero_or_drop_bucket(self, db_session):
        # The WHERE pct_used IS NOT NULL filter must still be applied — a null
        # sample in the bucket should not drag the bucket's MAX down.
        day_start = (_NOW - timedelta(days=2)).replace(hour=0, minute=0, second=0)
        _add_snap(db_session, ts=day_start + timedelta(hours=12), pct_used=30.0)
        # NULL pct_used row (a "no quota observed" sample)
        db_session.add(
            QuotaSnapshot(
                provider_id="gemini",
                account_id="acc1",
                window_type="daily",
                variant="",
                model_id="pro",
                ts=day_start + timedelta(hours=18),
                pct_used=None,
            )
        )
        db_session.commit()

        result = query_chart(db_session, days=90.0, metric="percent")
        pro = _pro_series(result)
        assert pro is not None
        assert pro["points"][0]["pct_used"] == 30.0
