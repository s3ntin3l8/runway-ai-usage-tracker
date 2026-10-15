"""Tests for app/services/data_health/checks/openrouter_model_events.py and
app/services/maintenance/openrouter_retag.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import select

from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.data_health.checks.openrouter_model_events import OpenRouterModelEventsCheck
from app.services.maintenance.openrouter_retag import NoTargetAccountError
from app.services.model_normalization import is_openrouter_model_id
from tests.unit.data_health.conftest import make_config, make_event

ME = "me@example.com"


def _check() -> OpenRouterModelEventsCheck:
    return OpenRouterModelEventsCheck()


def _target(session, account: str = ME) -> None:
    make_config(session, provider_id="openrouter", account_id=account)


def _ev(session, event_id, provider_id, model_id="tencent/hy3:free", **kw):
    kw.setdefault("account_id", "default" if provider_id == "opencode-byok" else ME)
    return make_event(session, event_id=event_id, provider_id=provider_id, model_id=model_id, **kw)


def _events(session):
    return {(e.provider_id, e.event_id): e for e in session.exec(select(UsageEvent)).all()}


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("tencent/hy3:free", True),
        ("qwen/qwen3.6-plus:free", True),
        ("openrouter/free", True),
        ("OpenRouter/Auto", True),
        ("qwen/qwen3-coder-30b", False),  # vendor-prefixed local model
        ("deepseek-v4-flash-free", False),  # Zen free tier, no vendor prefix
        ("mimo-v2.5-free", False),
        ("gpt-5.4", False),
        ("", False),
        (None, False),
    ],
)
def test_predicate(model_id, expected):
    assert is_openrouter_model_id(model_id) is expected


def test_sql_clause_agrees_with_the_python_predicate(session):
    models = ["tencent/hy3:free", "openrouter/free", "qwen/qwen3-coder-30b", "gpt-5.4", "x-free"]
    for i, model in enumerate(models):
        _ev(session, str(i), "opencode", model)

    report = _check().detect(session)

    moved = {s.detail["model_id"] for g in report.groups for s in g.samples}
    assert moved == {m for m in models if is_openrouter_model_id(m)}


def test_detect_groups_by_source_provider_and_ignores_other_events(session):
    _target(session)
    _ev(session, "1", "opencode")
    _ev(session, "2", "opencode-byok")
    _ev(session, "3", "opencode", "qwen/qwen3-coder-30b")
    _ev(session, "4", "openrouter")  # already correct

    report = _check().detect(session)

    assert {g.key: g.count for g in report.groups} == {"opencode": 1, "opencode-byok": 1}
    assert all(g.fixable and g.detail["target_account"] == ME for g in report.groups)


def test_group_is_not_fixable_without_a_single_target_account(session):
    _ev(session, "1", "opencode-byok")
    (group,) = _check().detect(session).groups
    assert not group.fixable

    _target(session, "a")
    _target(session, "b")
    (group,) = _check().detect(session).groups
    assert not group.fixable
    with pytest.raises(NoTargetAccountError):
        _check().plan(session, "opencode-byok", {})
    with pytest.raises(NoTargetAccountError):
        _check().apply(session, "opencode-byok", {})
    assert {e.provider_id for e in session.exec(select(UsageEvent)).all()} == {"opencode-byok"}


def test_plan_is_read_only_and_counts_collisions_with_openrouter(session):
    _target(session)
    _ev(session, "dup", "opencode")
    _ev(session, "dup", "opencode-byok")
    _ev(session, "only", "opencode-byok")

    plan = _check().plan(session, "opencode-byok", {})

    assert plan.counts == {"total": 2, "duplicates": 0, "to_move": 2}
    assert len(session.exec(select(UsageEvent)).all()) == 3


def test_applying_each_group_in_turn_collapses_byok_duplicates_of_opencode_rows(session):
    _target(session)
    _ev(session, "dup", "opencode", tokens_input=500)
    _ev(session, "dup", "opencode-byok", tokens_input=10)  # same message, thinner copy
    _ev(session, "byok-only", "opencode-byok")

    first, _ = _check().apply(session, "opencode-byok", {})
    assert first.counts["duplicates_resolved"] == 0
    assert set(_events(session)) == {
        ("opencode", "msg_dup"),
        ("openrouter", "msg_dup"),
        ("openrouter", "msg_byok-only"),
    }

    # The preview for the second group is exact: one collision, one copy dropped.
    assert _check().plan(session, "opencode", {}).counts["duplicates"] == 1
    second, _ = _check().apply(session, "opencode", {})

    events = _events(session)
    assert second.counts["duplicates_resolved"] == 1
    assert set(events) == {("openrouter", "msg_dup"), ("openrouter", "msg_byok-only")}
    assert events[("openrouter", "msg_dup")].tokens_input == 500  # the richer copy won
    assert {e.account_id for e in events.values()} == {ME}
    assert {e.attribution_source for e in events.values()} == {"tag"}
    assert _check().detect(session).total_count == 0


def test_apply_resolves_a_collision_with_an_existing_openrouter_row(session):
    _target(session)
    _ev(session, "same", "opencode", tokens_input=900)
    _ev(session, "same", "openrouter", tokens_input=5)

    _check().apply(session, "opencode", {})

    (only,) = session.exec(select(UsageEvent)).all()
    assert (only.provider_id, only.tokens_input) == ("openrouter", 900)


def test_apply_leaves_other_events_alone(session):
    _target(session)
    _ev(session, "1", "opencode")
    _ev(session, "2", "opencode", "qwen/qwen3-coder-30b")
    _ev(session, "3", "opencode-free", "mimo-v2.5-free")

    _check().apply(session, "opencode", {})

    assert set(_events(session)) == {
        ("openrouter", "msg_1"),
        ("opencode", "msg_2"),
        ("opencode-free", "msg_3"),
    }


def test_apply_preserves_a_source_reported_cost(session):
    _target(session)
    _ev(session, "1", "opencode", cost_usd=0.42)

    _check().apply(session, "opencode", {})

    (ev,) = session.exec(select(UsageEvent)).all()
    assert ev.cost_reported_usd == pytest.approx(0.42)


def test_apply_rebuilds_rollups_for_the_emptied_and_the_target_pair(session):
    _target(session)
    _ev(session, "1", "opencode-byok", ts=datetime(2026, 9, 1, tzinfo=UTC))

    _check().apply(session, "opencode-byok", {})

    rollups = session.exec(select(UsagePeriodRollup)).all()
    assert {r.provider_id for r in rollups} == {"openrouter"}
    assert {r.account_id for r in rollups} == {ME}


def test_apply_duplicate_loser_pair_rollups_are_rebuilt_too(session):
    """The copy under `opencode` loses; its pair must not keep stale rollups."""
    _target(session)
    _ev(session, "dup", "opencode", tokens_input=1)
    _ev(session, "dup", "opencode-byok", tokens_input=500)

    _check().apply(session, "opencode-byok", {})
    _check().apply(session, "opencode", {})

    assert {r.provider_id for r in session.exec(select(UsagePeriodRollup)).all()} == {"openrouter"}
