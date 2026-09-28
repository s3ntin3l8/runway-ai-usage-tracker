"""Tests for app/services/data_health/checks/config_default_keyed.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import select

from app.models.db import ProviderConfig, UsageEvent
from app.services.data_health.checks.config_default_keyed import ConfigDefaultKeyedCheck
from tests.unit.data_health.conftest import make_config


def _check() -> ConfigDefaultKeyedCheck:
    return ConfigDefaultKeyedCheck()


def test_detect_finds_a_default_keyed_config_with_a_real_label(session):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )

    report = _check().detect(session)

    assert report.total_count == 1
    assert report.groups[0].key == "minimax"
    assert report.groups[0].detail["suggested_new_account_id"] == "alice@example.com"


def test_detect_ignores_a_label_that_is_still_the_literal_string_default(session):
    make_config(session, provider_id="minimax", account_id="default", account_label="default")

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_an_archived_row(session):
    make_config(
        session,
        provider_id="minimax",
        account_id="default",
        account_label="alice@example.com",
        archived=True,
    )

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_a_row_with_no_label_at_all(session):
    make_config(session, provider_id="minimax", account_id="default")

    report = _check().detect(session)

    assert report.total_count == 0


def test_plan_is_read_only(session):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )

    _check().plan(session, "minimax", {})

    row = session.exec(select(ProviderConfig)).one()
    assert row.account_id == "default"


def test_apply_rekeys_the_config_and_clears_the_finding(session):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )

    result, hooks = _check().apply(session, "minimax", {})

    assert "alice@example.com" in result.summary
    assert len(hooks) == 1
    row = session.exec(select(ProviderConfig)).one()
    assert row.account_id == "alice@example.com"
    assert _check().detect(session).total_count == 0


def test_apply_moves_default_event_history_and_rebuilds_derived_data(session):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )
    session.add(
        UsageEvent(
            provider_id="minimax",
            account_id="default",
            sidecar_id="test-sidecar",
            event_id="event-1",
            ts=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    session.commit()

    result, _hooks = _check().apply(session, "minimax", {})

    event = session.exec(select(UsageEvent)).one()
    assert event.account_id == "alice@example.com"
    assert result.counts["usage_events_moved"] == 1


def test_apply_honors_an_explicit_new_account_id_override(session):
    make_config(session, provider_id="minimax", account_id="default", account_label="stale-label")

    result, _hooks = _check().apply(session, "minimax", {"new_account_id": "bob@example.com"})

    assert "bob@example.com" in result.summary
    row = session.exec(select(ProviderConfig)).one()
    assert row.account_id == "bob@example.com"


def test_apply_raises_on_collision_by_default(session):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    with pytest.raises(ValueError, match="already exists"):
        _check().apply(session, "minimax", {})


def test_apply_archives_the_source_row_on_collision_when_requested(session):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )
    make_config(session, provider_id="minimax", account_id="alice@example.com")

    with pytest.raises(ValueError, match="Confirm that the source and target"):
        _check().apply(session, "minimax", {"on_collision": "archive_default"})

    _check().apply(
        session,
        "minimax",
        {"on_collision": "archive_default", "same_account_confirmed": True},
    )

    default_row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "minimax", ProviderConfig.account_id == "default"
        )
    ).one()
    assert default_row.archived is True


def test_collision_preview_shows_both_identities_and_requires_attestation(session):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )
    make_config(
        session, provider_id="minimax", account_id="alice@example.com", account_label="Alice"
    )

    plan = _check().plan(session, "minimax", {"on_collision": "archive_default"})

    assert "same provider account" in plan.confirmation_text
    assert [sample.label for sample in plan.samples[:2]] == [
        "minimax/default (source)",
        "minimax/alice@example.com (target)",
    ]
    assert plan.counts["usage_events_to_move"] == 0
