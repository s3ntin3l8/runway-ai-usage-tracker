"""Tests for app/services/data_health/checks/minimax_background_sessions.py."""

from __future__ import annotations

import json

from app.services.data_health.checks.minimax_background_sessions import (
    MinimaxBackgroundSessionsCheck,
)
from tests.unit.data_health.conftest import make_event


def _provenance(*, source_session_id: str = "mvs_bg", parent_session_id: str = "mvs_main") -> str:
    return json.dumps(
        {
            "source_session_id": source_session_id,
            "parent_session_id": parent_session_id,
            "agent_name": "explore",
            "purpose": "local-background-task:bg_1234",
        }
    )


def _check() -> MinimaxBackgroundSessionsCheck:
    return MinimaxBackgroundSessionsCheck()


def _event(session, **overrides):
    base = {
        "provider_id": "minimax",
        "source_app": "minimax_code",
        "account_id": "acc1",
        "event_id": "e1",
        "session_id": "mvs_bg",
        "subagent_type": "Explore",
        "turn_id": "turn_task_bg_bg_1234",
        "raw_json": _provenance(),
    }
    base.update(overrides)
    return make_event(session, **base)


def test_detect_finds_a_row_filed_on_the_wrong_session(session):
    _event(session)

    report = _check().detect(session)

    assert report.total_count == 1
    group = report.groups[0]
    assert group.key == "mvs_bg->mvs_main"
    assert group.fixable is True
    assert group.detail["parent_session_id"] == "mvs_main"
    assert group.detail["usage_events"] == 1


def test_detect_ignores_a_row_filed_under_its_parent(session):
    _event(session, session_id="mvs_main")

    report = _check().detect(session)

    assert report.total_count == 0
    assert report.groups == []


def test_detect_ignores_a_row_without_a_provenance(session):
    """Rows collected before the extractor learned the parent link carry none."""
    _event(session, raw_json=None)

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_another_client_app(session):
    _event(session, source_app="minimax_api")

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_survives_a_malformed_raw_json(session):
    """A row whose raw_json is not JSON must not break the scan."""
    _event(session, event_id="broken", raw_json="not json at all")
    _event(session, event_id="e2")

    report = _check().detect(session)

    assert report.total_count == 1


def test_detect_groups_the_rows_of_one_background_task(session):
    _event(session, event_id="e1")
    _event(session, event_id="e2")

    report = _check().detect(session)

    assert len(report.groups) == 1
    assert report.groups[0].count == 2
    assert report.total_count == 2


def test_the_plan_reports_the_rows_it_would_move(session):
    _event(session, event_id="e1")
    _event(session, event_id="e2")

    plan = _check().plan(session, "mvs_bg->mvs_main", {})

    assert plan.counts["usage_events_to_move"] == 2
    assert "mvs_main" in plan.summary


def test_the_apply_files_the_row_under_its_parent(session):
    row = _event(session)

    result, hooks = _check().apply(session, "mvs_bg->mvs_main", {})

    assert result.counts["usage_events_moved"] == 1
    assert hooks == []
    stored = session.get(type(row), row.id)
    assert stored.session_id == "mvs_main"
    # Nothing else moves: the tag, the turn and the tokens stay as they were.
    assert stored.subagent_type == "Explore"
    assert stored.turn_id == "turn_task_bg_bg_1234"
    assert stored.tokens_input == 100
    assert stored.cost_usd == 0.05


def test_the_apply_is_idempotent(session):
    _event(session)
    _check().apply(session, "mvs_bg->mvs_main", {})

    report = _check().detect(session)

    assert report.total_count == 0
    result, _ = _check().apply(session, "mvs_bg->mvs_main", {})
    assert result.counts["usage_events_moved"] == 0


def test_the_apply_leaves_a_correctly_filed_row_alone(session):
    row = _event(session, session_id="mvs_main")

    result, _ = _check().apply(session, "mvs_main", {})

    assert result.counts["usage_events_moved"] == 0
    assert session.get(type(row), row.id).session_id == "mvs_main"


def test_detect_ignores_a_parent_that_is_not_text(session):
    """A payload may name a numeric or a nested parent — neither is a session."""
    _event(session, raw_json=json.dumps({"parent_session_id": 123}))
    _event(session, event_id="e2", raw_json=json.dumps({"parent_session_id": {"nested": "x"}}))

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_groups_by_the_parent_as_well_as_the_child(session):
    """The fix moves each row to *its own* named parent, so the preview has to
    name the same one."""
    _event(session, event_id="e1")
    _event(
        session,
        event_id="e2",
        raw_json=_provenance(parent_session_id="mvs_other"),
    )

    report = _check().detect(session)

    assert len(report.groups) == 2
    assert {g.key for g in report.groups} == {"mvs_bg->mvs_main", "mvs_bg->mvs_other"}
    assert all(g.count == 1 for g in report.groups)


def test_detect_accepts_a_legacy_source_app(session):
    """A row collected before the source_app column carries the provider id."""
    _event(session, source_app="minimax")

    report = _check().detect(session)

    assert report.total_count == 1


def test_the_plan_names_the_parent_it_moves_to(session):
    _event(session, event_id="e1")
    _event(
        session,
        event_id="e2",
        raw_json=_provenance(parent_session_id="mvs_other"),
    )

    plan = _check().plan(session, "mvs_bg->mvs_other", {})

    assert plan.counts["usage_events_to_move"] == 1
    assert "mvs_other" in plan.summary
    assert "mvs_main" not in plan.summary


def test_the_plan_of_a_filed_group_says_so(session):
    _event(session, event_id="e1")
    _check().apply(session, "mvs_bg->mvs_main", {})

    plan = _check().plan(session, "mvs_bg->mvs_main", {})

    assert plan.counts["usage_events_to_move"] == 0
    assert "already filed" in plan.summary
