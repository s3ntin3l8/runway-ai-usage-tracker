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


def test_plan_is_read_only(session):
    make_event(
        session, event_id="1", provider_id="minimax", account_id="alice@example.com", cost_usd=1.0
    )
    _lifetime_rollup(session, msgs=5, cost_usd=1.0)

    _check().plan(session, "minimax::alice@example.com", {})

    row = session.exec(select(UsagePeriodRollup)).one()
    assert row.msgs == 5  # untouched


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
