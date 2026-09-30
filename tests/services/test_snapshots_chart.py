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

from app.models.db import QuotaSnapshot, UsageEvent, UsagePeriodRollup
from app.services.queries._shared import sqlite_utc_timestamp
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


def test_sqlite_utc_timestamp_matches_stored_boundary_comparisons(db_session):
    stored = "2026-05-08 12:00:00.000000"
    boundary = sqlite_utc_timestamp(datetime(2026, 5, 8, 12, tzinfo=UTC))

    assert boundary == stored
    comparisons = (
        db_session.connection()
        .exec_driver_sql(
            "SELECT :stored >= :since, :stored < :until",
            {"stored": stored, "since": boundary, "until": boundary},
        )
        .one()
    )
    assert comparisons[0]  # an event at `since` is included
    assert not comparisons[1]  # an event at `until` is excluded


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
        # >90-day window → 1-day buckets (86400s; the 30-90d range now uses a
        # 6-hour tier instead, see _BUCKET_TIERS). Place a peak well inside the
        # UTC day and a 0 sample near the end of the same day.
        day_start = (_NOW - timedelta(days=2)).replace(hour=0, minute=0, second=0)
        _add_snap(db_session, ts=day_start + timedelta(hours=12), pct_used=46.0)
        _add_snap(db_session, ts=day_start + timedelta(hours=23, minutes=55), pct_used=0.0)

        result = query_chart(db_session, days=120.0, metric="percent")
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

        result = query_chart(db_session, days=120.0, metric="percent")
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

        result = query_chart(db_session, days=120.0, metric="percent")
        pro = _pro_series(result)
        assert pro is not None
        pcts = sorted(p["pct_used"] for p in pro["points"])
        assert pcts == [20.0, 46.0]


class TestNinetyDayWindowUsesSixHourBuckets:
    """Regression test for the History tab 90d-chart-too-coarse fix: the
    30-90d tier must use 6-hour buckets, not collapse to 1-day buckets,
    so a weekly reset sawtooth within a single UTC day stays visible."""

    def test_same_day_samples_in_different_six_hour_buckets_both_survive(self, db_session):
        # Two samples 12 hours apart on the same UTC day land in different
        # 6-hour buckets (00-06/06-12/12-18/18-24), so both peaks surface —
        # at the old 1-day tier these would collapse into a single point.
        day_start = (_NOW - timedelta(days=5)).replace(hour=0, minute=0, second=0)
        _add_snap(db_session, ts=day_start + timedelta(hours=2), pct_used=90.0)
        _add_snap(db_session, ts=day_start + timedelta(hours=14), pct_used=15.0)

        result = query_chart(db_session, days=90.0, metric="percent")
        pro = _pro_series(result)
        assert pro is not None
        pcts = sorted(p["pct_used"] for p in pro["points"])
        assert pcts == [15.0, 90.0]


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


def _add_rollup(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    day: str,
    model_id: str = "",
    tokens_input: int = 0,
    cost_usd: float = 0.0,
    cost_cache_read: float = 0.0,
    cost_cache_create: float = 0.0,
) -> None:
    session.add(
        UsagePeriodRollup(
            provider_id=provider_id,
            account_id=account_id,
            period_type="day",
            period_key=day,
            model_id=model_id,
            sidecar_id="",
            tokens_input=tokens_input,
            cost_usd=cost_usd,
            cost_cache_read=cost_cache_read,
            cost_cache_create=cost_cache_create,
            last_updated=_NOW,
        )
    )
    session.commit()


def _add_chart_event(
    session: Session,
    *,
    event_id: str,
    ts: datetime,
    tokens_input: int,
    cost_usd: float = 0.0,
    cost_cache_read: float = 0.0,
) -> None:
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="acc1",
            sidecar_id="dev-01",
            event_id=event_id,
            ts=ts,
            model_id="sonnet",
            tokens_input=tokens_input,
            cost_usd=cost_usd,
            cost_cache_read=cost_cache_read,
        )
    )
    session.commit()


class TestChartPartialDayBoundaries:
    def test_daily_chart_combines_exact_partial_days_with_full_day_rollups(self, db_session):
        since = datetime(2026, 4, 1, 12, tzinfo=UTC)
        until = datetime(2026, 4, 4, 18, tzinfo=UTC)
        _add_chart_event(
            db_session,
            event_id="before-start",
            ts=datetime(2026, 4, 1, 11, 59, tzinfo=UTC),
            tokens_input=999,
        )
        _add_rollup(
            db_session,
            provider_id="anthropic",
            account_id="acc1",
            day="2026-04-01",
            model_id="sonnet",
            tokens_input=900,
            cost_usd=90.0,
        )
        _add_chart_event(
            db_session,
            event_id="start-day",
            ts=datetime(2026, 4, 1, 12, tzinfo=UTC),
            tokens_input=10,
            cost_usd=1.0,
            cost_cache_read=0.25,
        )
        _add_rollup(
            db_session,
            provider_id="anthropic",
            account_id="acc1",
            day="2026-04-02",
            model_id="sonnet",
            tokens_input=20,
            cost_usd=2.0,
        )
        _add_rollup(
            db_session,
            provider_id="anthropic",
            account_id="acc1",
            day="2026-04-03",
            model_id="sonnet",
            tokens_input=30,
            cost_usd=3.0,
        )
        _add_chart_event(
            db_session,
            event_id="end-day",
            ts=datetime(2026, 4, 4, 17, 59, tzinfo=UTC),
            tokens_input=40,
            cost_usd=4.0,
            cost_cache_read=1.0,
        )
        _add_chart_event(
            db_session,
            event_id="at-end",
            ts=until,
            tokens_input=1000,
        )

        tokens = query_chart(
            db_session,
            metric="tokens",
            since=since,
            until=until,
            provider_id="anthropic",
            account_id="acc1",
        )
        assert [bar["date"] for bar in tokens["bars"]] == [
            "2026-04-01",
            "2026-04-02",
            "2026-04-03",
            "2026-04-04",
        ]
        # The 900-token daily rollup includes usage before `since`; partial
        # boundary days must be rebuilt from in-range events, not added whole.
        assert [bar["segments"][0]["value"] for bar in tokens["bars"]] == [10, 20, 30, 40]

        costs = query_chart(
            db_session,
            metric="cost",
            since=since,
            until=until,
            provider_id="anthropic",
            account_id="acc1",
        )
        assert [bar["segments"][0]["value"] for bar in costs["bars"]] == [1.0, 2.0, 3.0, 4.0]
        assert [bar["segments"][0]["value_cache"] for bar in costs["bars"]] == [0.25, 0, 0, 1.0]

    def test_same_day_range_is_counted_once_and_excludes_until(self, db_session):
        since = datetime(2026, 4, 5, 10, tzinfo=UTC)
        until = datetime(2026, 4, 5, 18, tzinfo=UTC)
        _add_chart_event(
            db_session,
            event_id="within",
            ts=datetime(2026, 4, 5, 12, tzinfo=UTC),
            tokens_input=12,
        )
        _add_chart_event(db_session, event_id="excluded", ts=until, tokens_input=100)

        result = query_chart(
            db_session,
            metric="tokens",
            since=since,
            until=until,
            provider_id="anthropic",
            account_id="acc1",
        )
        assert len(result["bars"]) == 1
        assert result["bars"][0]["date"] == "2026-04-05"
        assert result["bars"][0]["segments"][0]["value"] == 12


class TestGroupByProvider:
    """group="provider" collapses token/cost segments to one per provider per
    bar — summing across accounts and models — for the cross-provider view."""

    _SINCE = datetime(2026, 4, 1, tzinfo=UTC)
    _UNTIL = datetime(2026, 5, 1, tzinfo=UTC)

    def test_one_segment_per_provider_summing_accounts_and_models(self, db_session):
        # anthropic: two accounts, one with a per-model row — all must fold into
        # a single "Anthropic" segment. openai: a separate segment.
        _add_rollup(
            db_session,
            provider_id="anthropic",
            account_id="acc1",
            day="2026-04-05",
            model_id="opus",
            tokens_input=100,
        )
        _add_rollup(
            db_session,
            provider_id="anthropic",
            account_id="acc1",
            day="2026-04-05",
            model_id="sonnet",
            tokens_input=30,
        )
        _add_rollup(
            db_session,
            provider_id="anthropic",
            account_id="acc2",
            day="2026-04-05",
            tokens_input=20,
        )
        _add_rollup(
            db_session,
            provider_id="openai",
            account_id="acc1",
            day="2026-04-05",
            tokens_input=70,
        )
        result = query_chart(
            db_session,
            metric="tokens",
            since=self._SINCE,
            until=self._UNTIL,
            group="provider",
        )
        segs = {s["provider_id"]: s for s in result["bars"][0]["segments"]}
        assert set(segs) == {"anthropic", "openai"}
        assert segs["anthropic"]["value"] == 150  # 100 + 30 + 20
        assert segs["anthropic"]["model_id"] == ""
        assert segs["anthropic"]["label"] == "Anthropic"
        assert segs["openai"]["value"] == 70

    def test_cost_value_cache_summed_per_provider(self, db_session):
        _add_rollup(
            db_session,
            provider_id="anthropic",
            account_id="acc1",
            day="2026-04-05",
            cost_usd=10.0,
            cost_cache_read=2.0,
            cost_cache_create=1.0,
        )
        _add_rollup(
            db_session,
            provider_id="anthropic",
            account_id="acc2",
            day="2026-04-05",
            cost_usd=5.0,
            cost_cache_read=0.5,
            cost_cache_create=0.0,
        )
        result = query_chart(
            db_session,
            metric="cost",
            since=self._SINCE,
            until=self._UNTIL,
            group="provider",
        )
        seg = result["bars"][0]["segments"][0]
        assert seg["value"] == 15.0
        assert seg["value_cache"] == 3.5  # 2.0 + 1.0 + 0.5

    def test_no_group_keeps_per_model_segments(self, db_session):
        # Sanity: without group, the per-account/per-model behavior is unchanged.
        _add_rollup(
            db_session,
            provider_id="anthropic",
            account_id="acc1",
            day="2026-04-05",
            model_id="opus",
            tokens_input=100,
        )
        _add_rollup(
            db_session,
            provider_id="anthropic",
            account_id="acc1",
            day="2026-04-05",
            model_id="sonnet",
            tokens_input=30,
        )
        result = query_chart(
            db_session,
            metric="tokens",
            since=self._SINCE,
            until=self._UNTIL,
        )
        labels = sorted(s["label"] for s in result["bars"][0]["segments"])
        assert labels == ["Anthropic · opus", "Anthropic · sonnet"]
