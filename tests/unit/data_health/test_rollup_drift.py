"""Tests for app/services/data_health/checks/rollup_drift.py."""

from __future__ import annotations

from sqlmodel import select

from app.models.db import UsagePeriodRollup
from app.services.data_health.checks.rollup_drift import RollupDriftCheck
from tests.unit.data_health.conftest import make_event


def _check() -> RollupDriftCheck:
    return RollupDriftCheck()


def _lifetime_rollup(session, **overrides) -> UsagePeriodRollup:
    base = {
        "provider_id": "minimax",
        "account_id": "alice@example.com",
        "period_type": "lifetime",
        "period_key": "all",
        "model_id": "",
        "sidecar_id": "",
        "msgs": 0,
        "cost_usd": 0.0,
    }
    base.update(overrides)
    row = UsagePeriodRollup(**base)
    session.add(row)
    session.commit()
    return row


def test_detect_finds_no_drift_when_rollup_matches_events(session):
    make_event(
        session, event_id="1", provider_id="minimax", account_id="alice@example.com", cost_usd=1.0
    )
    _lifetime_rollup(session, msgs=1, cost_usd=1.0)

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_finds_a_msgs_drift(session):
    make_event(
        session, event_id="1", provider_id="minimax", account_id="alice@example.com", cost_usd=1.0
    )
    _lifetime_rollup(session, msgs=5, cost_usd=1.0)  # stale — should be 1

    report = _check().detect(session)

    assert report.total_count == 4  # |1 - 5| — the drift magnitude
    assert report.groups[0].detail["actual_msgs"] == 1
    assert report.groups[0].detail["rollup_msgs"] == 5


def test_detect_finds_a_cost_drift(session):
    make_event(
        session, event_id="1", provider_id="minimax", account_id="alice@example.com", cost_usd=1.0
    )
    _lifetime_rollup(session, msgs=1, cost_usd=99.0)

    report = _check().detect(session)

    assert report.total_count == 1


def test_detect_treats_a_missing_rollup_row_as_full_drift(session):
    make_event(
        session, event_id="1", provider_id="minimax", account_id="alice@example.com", cost_usd=1.0
    )
    # No rollup row at all.

    report = _check().detect(session)

    assert report.total_count == 1
    assert report.groups[0].detail["rollup_msgs"] == 0


def test_detect_finds_stale_rollup_when_events_are_gone(session):
    _lifetime_rollup(session, msgs=8, cost_usd=2.5)

    report = _check().detect(session)

    assert report.total_count == 8
    assert report.groups[0].detail["actual_msgs"] == 0
    assert report.groups[0].detail["rollup_msgs"] == 8


def test_apply_removes_rollup_rows_when_events_are_gone(session):
    _lifetime_rollup(session, msgs=8, cost_usd=2.5)

    _check().apply(session, "minimax::alice@example.com", {})

    assert session.exec(select(UsagePeriodRollup)).all() == []
    assert _check().detect(session).total_count == 0


def test_plan_is_read_only(session):
    make_event(
        session, event_id="1", provider_id="minimax", account_id="alice@example.com", cost_usd=1.0
    )
    _lifetime_rollup(session, msgs=5, cost_usd=1.0)

    _check().plan(session, "minimax::alice@example.com", {})

    row = session.exec(select(UsagePeriodRollup)).one()
    assert row.msgs == 5  # untouched


def test_detect_query_count_is_constant_regardless_of_pair_count(session, query_counter):
    """detect() must issue the same number of queries whether there's 1 pair
    or 10 — the whole point of #375's grouped-query rewrite. The old
    per-pair implementation issued 1 + 2*N queries (pair-set query, then
    _actual_totals + _rollup_totals per pair); the new one issues exactly 2
    (one grouped actual-totals query, one grouped rollup query) no matter N.
    """
    make_event(session, event_id="baseline", provider_id="minimax", account_id="solo@example.com")
    _lifetime_rollup(session, account_id="solo@example.com", msgs=1, cost_usd=0.05)
    query_counter.reset()
    _check().detect(session)
    baseline_count = query_counter.count

    session2_pairs = [(f"provider{i}", f"account{i}@example.com") for i in range(10)]
    for provider_id, account_id in session2_pairs:
        make_event(
            session,
            event_id=f"{provider_id}-{account_id}",
            provider_id=provider_id,
            account_id=account_id,
            cost_usd=0.05,
        )
        _lifetime_rollup(
            session, provider_id=provider_id, account_id=account_id, msgs=1, cost_usd=0.05
        )
    query_counter.reset()
    _check().detect(session)
    ten_pair_count = query_counter.count

    assert baseline_count == 2
    assert ten_pair_count == baseline_count


def test_apply_rebuilds_the_rollup_and_clears_the_finding(session):
    make_event(
        session, event_id="1", provider_id="minimax", account_id="alice@example.com", cost_usd=1.0
    )
    _lifetime_rollup(session, msgs=5, cost_usd=1.0)

    result, hooks = _check().apply(session, "minimax::alice@example.com", {})

    assert result.counts["msgs_after"] == 1
    assert hooks == []
    row = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.period_key == "all",
        )
    ).one()
    assert row.msgs == 1
    assert _check().detect(session).total_count == 0
