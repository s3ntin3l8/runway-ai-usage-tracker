"""Tests for app/services/data_health/checks/orphan_gauge_series.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.models.db import LatestUsage, QuotaSnapshot
from app.services.data_health.checks.orphan_gauge_series import OrphanGaugeSeriesCheck
from tests.unit.data_health.conftest import (
    make_config,
    make_event,
    make_latest_usage,
    make_snapshot,
)


def _check() -> OrphanGaugeSeriesCheck:
    return OrphanGaugeSeriesCheck()


def test_detect_finds_an_unconfigured_stale_series(session):
    make_latest_usage(
        session,
        provider_id="minimax",
        account_id="default",
        updated_at=datetime.now(UTC) - timedelta(days=90),
    )

    report = _check().detect(session)

    assert report.total_count == 1
    provider_id, account_id = report.groups[0].key.split("::")
    assert (provider_id, account_id) == ("minimax", "default")


def test_detect_ignores_recent_gauge_activity_without_usage_events(session):
    make_latest_usage(session, provider_id="minimax", account_id="default")

    assert _check().detect(session).total_count == 0


def test_detect_ignores_a_series_with_a_configured_account(session):
    make_config(session, provider_id="minimax", account_id="default")
    make_latest_usage(session, provider_id="minimax", account_id="default")

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_a_series_still_posting_recent_events(session):
    make_latest_usage(session, provider_id="minimax", account_id="default")
    make_event(
        session,
        event_id="1",
        provider_id="minimax",
        account_id="default",
        ts=datetime.now(UTC) - timedelta(days=1),
    )

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_flags_a_series_whose_events_are_all_old(session):
    make_latest_usage(
        session,
        provider_id="minimax",
        account_id="default",
        updated_at=datetime.now(UTC) - timedelta(days=90),
    )
    make_event(
        session,
        event_id="1",
        provider_id="minimax",
        account_id="default",
        ts=datetime.now(UTC) - timedelta(days=90),
    )

    report = _check().detect(session)

    assert report.total_count == 1


def test_detect_query_count_is_roughly_constant_regardless_of_pair_count(session, query_counter):
    """detect() must issue a small, bounded number of queries whether
    there's 1 orphaned pair or 10 — the whole point of #375's rewrite. The
    old per-pair implementation issued 2 + 5*N queries (2 for the pair set,
    then _has_config + _last_event_ts + _count_latest + _count_snapshots +
    candidate_targets per pair). The new one issues 4 fixed grouped queries
    plus exactly one candidate_targets call per *distinct provider_id*
    among the orphaned pairs (memoized, not per pair) — so growth tracks
    the number of providers, never the number of pairs.
    """
    stale_ts = datetime.now(UTC) - timedelta(days=90)
    make_latest_usage(session, provider_id="minimax", account_id="solo")
    make_event(session, event_id="baseline", provider_id="minimax", account_id="solo", ts=stale_ts)
    query_counter.reset()
    _check().detect(session)
    baseline_count = query_counter.count

    # 10 orphaned pairs across 2 distinct providers, to exercise the
    # candidate_targets memoization (one call per provider, not per pair).
    for i in range(10):
        provider_id = "minimax" if i % 2 == 0 else "chatgpt"
        account_id = f"account{i}"
        make_latest_usage(session, provider_id=provider_id, account_id=account_id)
        make_event(
            session,
            event_id=f"{provider_id}-{account_id}",
            provider_id=provider_id,
            account_id=account_id,
            ts=stale_ts,
        )
    query_counter.reset()
    _check().detect(session)
    ten_pair_count = query_counter.count

    # Query cost stays constant as the number of pairs and providers grows;
    # all per-pair counts, config checks, and activity timestamps are grouped.
    assert baseline_count == 6
    assert ten_pair_count == 6


def test_plan_is_read_only(session):
    make_latest_usage(
        session,
        provider_id="minimax",
        account_id="default",
        updated_at=datetime.now(UTC) - timedelta(days=90),
    )

    _check().plan(session, "minimax::default", {"action": "delete"})

    assert session.exec(select(LatestUsage)).one() is not None


def test_apply_delete_removes_both_tables(session):
    make_latest_usage(
        session,
        provider_id="minimax",
        account_id="default",
        updated_at=datetime.now(UTC) - timedelta(days=90),
    )
    make_snapshot(
        session, provider_id="minimax", account_id="default", ts=datetime(2026, 6, 1, tzinfo=UTC)
    )

    result, hooks = _check().apply(session, "minimax::default", {"action": "delete"})

    assert result.counts["latest_usage_deleted"] == 1
    assert result.counts["snapshots_deleted"] == 1
    assert hooks == []
    assert list(session.exec(select(LatestUsage))) == []
    assert list(session.exec(select(QuotaSnapshot))) == []
    assert _check().detect(session).total_count == 0


def test_apply_rejects_series_that_became_active(session):
    make_latest_usage(
        session,
        provider_id="minimax",
        account_id="default",
        updated_at=datetime.now(UTC) - timedelta(days=90),
    )
    make_event(
        session,
        event_id="recent",
        provider_id="minimax",
        account_id="default",
        ts=datetime.now(UTC) - timedelta(days=1),
    )

    with pytest.raises(ValueError, match="recent activity"):
        _check().apply(session, "minimax::default", {"action": "delete"})


def test_apply_merge_folds_into_a_configured_account(session):
    make_config(session, provider_id="minimax", account_id="alice@example.com")
    make_latest_usage(
        session,
        provider_id="minimax",
        account_id="default",
        updated_at=datetime.now(UTC) - timedelta(days=90),
        card_json='{"pct_used": 5.0}',
    )

    result, _hooks = _check().apply(
        session, "minimax::default", {"action": "merge", "target": "alice@example.com"}
    )

    assert result.counts["retagged"] == 1
    row = session.exec(select(LatestUsage)).one()
    assert row.account_id == "alice@example.com"


def test_apply_merge_without_a_valid_target_raises(session):
    make_latest_usage(
        session,
        provider_id="minimax",
        account_id="default",
        updated_at=datetime.now(UTC) - timedelta(days=90),
    )

    with pytest.raises(ValueError, match="merge requires a target"):
        _check().apply(session, "minimax::default", {"action": "merge"})
