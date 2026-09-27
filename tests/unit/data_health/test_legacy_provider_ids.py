"""Tests for app/services/data_health/checks/legacy_provider_ids.py."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import select

from app.models.db import UsageEvent
from app.services.data_health.checks.legacy_provider_ids import LegacyProviderIdsCheck
from tests.unit.data_health.conftest import make_event


def _check() -> LegacyProviderIdsCheck:
    return LegacyProviderIdsCheck()


def test_detect_finds_events_under_a_legacy_provider_id(session):
    make_event(session, event_id="1", provider_id="opencode-xai", model_id="grok")

    report = _check().detect(session)

    assert report.total_count == 1
    assert report.groups[0].key == "opencode-xai"
    assert report.groups[0].detail["canonical_provider_id"] == "xai"


def test_detect_ignores_providers_not_in_the_legacy_map(session):
    make_event(session, event_id="1", provider_id="xai", model_id="grok")

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_reports_a_collision_sub_count(session):
    make_event(session, event_id="dup", provider_id="opencode-xai", model_id="grok")
    make_event(session, event_id="dup", provider_id="xai", model_id="grok")

    report = _check().detect(session)

    assert report.groups[0].detail["collisions"] == 1


def test_plan_is_read_only(session):
    make_event(session, event_id="1", provider_id="opencode-xai", model_id="grok")

    _check().plan(session, "opencode-xai", {})

    ev = session.exec(select(UsageEvent)).one()
    assert ev.provider_id == "opencode-xai"


def test_apply_retags_onto_the_canonical_provider_and_clears_the_finding(session):
    make_event(session, event_id="1", provider_id="opencode-xai", model_id="grok")

    result, hooks = _check().apply(session, "opencode-xai", {})

    assert result.counts["retagged"] == 1
    assert hooks == []
    ev = session.exec(select(UsageEvent)).one()
    assert ev.provider_id == "xai"
    assert _check().detect(session).total_count == 0


def test_apply_resolves_a_collision(session):
    make_event(
        session,
        event_id="dup",
        provider_id="opencode-xai",
        model_id="grok",
        tokens_input=1000,
        ts=datetime(2026, 9, 1, tzinfo=UTC),
    )
    make_event(
        session,
        event_id="dup",
        provider_id="xai",
        model_id="grok",
        tokens_input=10,
        ts=datetime(2026, 9, 1, tzinfo=UTC),
    )

    result, _hooks = _check().apply(session, "opencode-xai", {})

    assert result.counts["collisions_resolved"] == 1
    rows = list(session.exec(select(UsageEvent)))
    assert len(rows) == 1  # the loser was dropped
    assert rows[0].provider_id == "xai"
    assert rows[0].tokens_input == 1000  # the richer (legacy) row won
