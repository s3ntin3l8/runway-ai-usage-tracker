"""Tests for app/services/data_health/checks/lone_default_events.py."""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.models.db import UsageEvent
from app.services.data_health.checks.lone_default_events import LoneDefaultEventsCheck
from tests.unit.data_health.conftest import make_config, make_event


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


def test_detect_marks_not_fixable_with_no_candidate_account(session):
    """The opencode-byok shape: lone default events, no other config to
    reassign onto."""
    make_event(session, event_id="1", provider_id="opencode-byok", account_id="default")

    report = _check().detect(session)

    group = report.groups[0]
    assert group.fixable is False
    assert "no configured" in group.not_fixable_reason


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

    assert result.counts["moved"] == 1
    assert hooks == []
    ev = session.exec(select(UsageEvent)).one()
    assert ev.account_id == "alice@example.com"
    assert _check().detect(session).total_count == 0


def test_apply_rejects_target_equal_to_default(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    with pytest.raises(ValueError, match="cannot be 'default'"):
        _check().apply(session, "minimax", {"target": "default"})


def test_apply_rejects_a_target_with_no_provider_config(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    with pytest.raises(ValueError, match="not a configured account"):
        _check().apply(session, "minimax", {"target": "not-configured@example.com"})


def test_apply_requires_an_explicit_target_when_ambiguous(session):
    make_event(session, event_id="1", provider_id="minimax", account_id="default")
    make_config(session, provider_id="minimax", account_id="alice@example.com")
    make_config(session, provider_id="minimax", account_id="bob@example.com")

    with pytest.raises(ValueError, match="no unambiguous target"):
        _check().apply(session, "minimax", {})

    result, _hooks = _check().apply(session, "minimax", {"target": "bob@example.com"})
    assert result.counts["moved"] == 1
