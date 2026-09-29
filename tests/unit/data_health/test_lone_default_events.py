"""Tests for app/services/data_health/checks/lone_default_events.py."""

from __future__ import annotations

import json

import pytest
from sqlmodel import select

from app.models.db import LatestUsageContribution, UsageEvent
from app.services.data_health.checks.lone_default_events import LoneDefaultEventsCheck
from tests.unit.data_health.conftest import (
    make_config,
    make_event,
    make_latest_usage,
    make_snapshot,
)


def _check() -> LoneDefaultEventsCheck:
    return LoneDefaultEventsCheck()


def test_detect_finds_lone_default_events_with_one_unambiguous_target(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    report = _check().detect(session)

    assert report.total_count == 1
    group = report.groups[0]
    assert group.fixable is True
    assert group.detail["suggested_target"] == "alice@example.com"


def test_detect_offers_explicit_history_merge_when_an_active_default_config_exists(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="default", account_label="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    report = _check().detect(session)
    assert report.total_count == 1
    assert (
        _check().plan(session, "minimax", {"target": "alice@example.com"}).counts["usage_events"]
        == 1
    )


def test_detect_ignores_default_only_config_without_a_specific_target(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="default", account_label="default")

    report = _check().detect(session)

    assert report.total_count == 0
    assert report.groups == []


def test_detect_marks_not_fixable_with_no_candidate_account(session):
    """The opencode-byok shape: lone default events, no other config to
    reassign onto."""
    make_event(session, event_id="1", provider_id="opencode-byok", account_id="default")

    report = _check().detect(session)

    group = report.groups[0]
    assert group.fixable is False
    assert "no known" in group.not_fixable_reason


def test_detect_marks_not_fixable_when_multiple_candidates_are_ambiguous(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")
    make_config(session, provider_id="minimax", account_id="bob@example.com")

    report = _check().detect(session)

    group = report.groups[0]
    assert group.fixable is True  # candidates exist — client must specify one
    assert group.params[0].options == ["alice@example.com", "bob@example.com"]


def test_detect_breaks_down_by_kind(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default", kind="message")
    make_event(session, event_id="2", provider_id="minimax", account_id="default", kind="error")
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    report = _check().detect(session)

    assert report.groups[0].detail["by_kind"] == {"message": 1, "error": 1}


def test_plan_is_read_only(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    _check().plan(session, "minimax", {})

    ev = session.exec(select(UsageEvent)).one()
    assert ev.account_id == "default"


def test_apply_reassigns_onto_the_unambiguous_target(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    result, hooks = _check().apply(session, "minimax", {})

    assert result.counts["usage_events_moved"] == 1
    assert hooks == []
    ev = session.exec(select(UsageEvent)).one()
    assert ev.account_id == "alice@example.com"
    assert _check().detect(session).total_count == 0


def test_apply_moves_default_quota_cards_snapshots_and_contributions(session):
    from datetime import UTC, datetime

    from app.models.db import LatestUsage, QuotaSnapshot

    target = "alice@example.com"
    make_config(session, provider_id="minimax", account_id=target)
    make_latest_usage(session, provider_id="minimax", account_id="default")
    make_snapshot(
        session,
        provider_id="minimax",
        account_id="default",
        ts=datetime(2026, 9, 1, tzinfo=UTC),
    )
    session.add(
        LatestUsageContribution(
            provider_id="minimax",
            account_id="default",
            source_id="server:minimax",
            window_type="daily",
            variant="",
            model_id="",
            card_json='{"account_id":"default","pct_used":10}',
        )
    )
    session.commit()

    plan = _check().plan(session, "minimax", {"target": target})
    assert plan.counts["quota_cards_retagged"] == 1
    assert plan.counts["quota_contributions_retagged"] == 1
    assert plan.counts["quota_snapshots_retagged"] == 1

    _check().apply(session, "minimax", {"target": target})

    assert session.exec(select(LatestUsage).where(LatestUsage.account_id == "default")).all() == []
    assert (
        session.exec(select(QuotaSnapshot).where(QuotaSnapshot.account_id == "default")).all() == []
    )
    contribution = session.exec(select(LatestUsageContribution)).one()
    assert contribution.account_id == target


def test_apply_merges_colliding_quota_contributions_and_keeps_newest_update(session):
    from datetime import UTC, datetime

    target = "alice@example.com"
    make_config(session, provider_id="minimax", account_id=target)
    session.add_all(
        [
            LatestUsageContribution(
                provider_id="minimax",
                account_id=target,
                source_id="server:minimax",
                window_type="daily",
                variant="",
                model_id="",
                card_json='{"account_id":"alice@example.com","account_label":"Alice","target_only":true}',
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            LatestUsageContribution(
                provider_id="minimax",
                account_id="default",
                source_id="server:minimax",
                window_type="daily",
                variant="",
                model_id="",
                card_json='{"account_id":"default","source_only":true}',
                updated_at=datetime(2026, 2, 1, tzinfo=UTC),
            ),
        ]
    )
    session.commit()

    _check().apply(session, "minimax", {"target": target})

    contribution = session.exec(select(LatestUsageContribution)).one()
    card = json.loads(contribution.card_json)
    assert contribution.account_id == target
    assert contribution.updated_at.replace(tzinfo=UTC) == datetime(2026, 2, 1, tzinfo=UTC)
    assert card["account_id"] == target
    assert card["target_only"] is True
    assert card["source_only"] is True


def test_apply_rejects_target_equal_to_default(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    with pytest.raises(ValueError, match="cannot be 'default'"):
        _check().apply(session, "minimax", {"target": "default"})


def test_apply_rejects_a_target_with_no_provider_config(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    with pytest.raises(ValueError, match="not a known account"):
        _check().apply(session, "minimax", {"target": "not-configured@example.com"})


def test_apply_requires_an_explicit_target_when_ambiguous(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")
    make_config(session, provider_id="minimax", account_id="bob@example.com")

    with pytest.raises(ValueError, match="no unambiguous target"):
        _check().apply(session, "minimax", {})

    result, _hooks = _check().apply(session, "minimax", {"target": "bob@example.com"})
    assert result.counts["usage_events_moved"] == 1
