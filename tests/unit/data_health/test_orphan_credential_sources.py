"""Tests for app/services/data_health/checks/orphan_credential_sources.py."""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.models.db import CredentialSource
from app.services.data_health.checks.orphan_credential_sources import (
    OrphanCredentialSourcesCheck,
)
from tests.unit.data_health.conftest import make_config, make_source


def _check() -> OrphanCredentialSourcesCheck:
    return OrphanCredentialSourcesCheck()


def test_detect_finds_a_config_row_whose_config_is_gone(session):
    make_source(
        session,
        provider_id="deepseek",
        account_id="bd6d58cf",
        source_id="config:deepseek:bd6d58cf",
    )

    report = _check().detect(session)

    assert report.total_count == 1
    (group,) = report.groups
    assert group.key == "deepseek"
    assert group.fixable is True
    assert group.params == []  # delete-only: nothing for the dialog to ask
    assert group.detail == {"config_ghosts": 1, "duplicates": 0}
    # blocked_by is filled in by jobs.py at report time, not by detect()
    assert _check().blocked_by == ("config_default_keyed",)


def test_detect_ignores_a_config_row_that_has_its_config(session):
    make_config(session, provider_id="deepseek", account_id="alice@example.com")
    make_source(
        session,
        provider_id="deepseek",
        account_id="alice@example.com",
        source_id="config:deepseek:alice@example.com",
    )

    assert _check().detect(session).total_count == 0


def test_detect_groups_duplicates_on_the_account_without_evidence(session):
    make_config(session, provider_id="deepseek", account_id="alice@example.com")
    make_source(session, provider_id="deepseek", account_id="bd6d58cf", source_id="sidecar:abc")
    make_source(
        session,
        provider_id="deepseek",
        account_id="alice@example.com",
        source_id="sidecar:abc",
    )

    report = _check().detect(session)

    assert report.total_count == 1
    (group,) = report.groups
    assert group.detail == {"config_ghosts": 0, "duplicates": 1}
    assert group.samples[0].detail["account_id"] == "bd6d58cf"


def test_plan_is_read_only_and_requires_findings(session):
    make_source(
        session,
        provider_id="deepseek",
        account_id="bd6d58cf",
        source_id="config:deepseek:bd6d58cf",
    )

    plan = _check().plan(session, "deepseek", {})

    assert plan.counts["sources_deleted"] == 1
    assert plan.confirmation_text is not None
    assert plan.samples[0].label == "Manual configuration · bd6d58cf"
    assert len(session.exec(select(CredentialSource)).all()) == 1

    with pytest.raises(ValueError, match="No stranded credential sources"):
        _check().plan(session, "openrouter", {})


def test_apply_requires_the_confirmation_and_deletes_the_rows(session):
    make_source(
        session,
        provider_id="deepseek",
        account_id="bd6d58cf",
        source_id="config:deepseek:bd6d58cf",
    )

    with pytest.raises(ValueError, match="Confirm the stranded rows"):
        _check().apply(session, "deepseek", {})

    result, hooks = _check().apply(session, "deepseek", {"same_account_confirmed": True})

    assert result.counts == {"config_ghosts": 1, "duplicates": 0, "sources_deleted": 1}
    assert hooks == []
    assert session.exec(select(CredentialSource)).all() == []
    assert _check().detect(session).total_count == 0


def test_apply_with_a_stale_group_deletes_nothing(session):
    """Apply re-derives the rows itself, so a group that was fixed between
    preview and apply is a no-op rather than a blanket delete."""
    make_config(session, provider_id="deepseek", account_id="alice@example.com")
    make_source(
        session,
        provider_id="deepseek",
        account_id="alice@example.com",
        source_id="config:deepseek:alice@example.com",
    )

    result, _hooks = _check().apply(session, "deepseek", {"same_account_confirmed": True})

    assert result.counts["sources_deleted"] == 0
    assert len(session.exec(select(CredentialSource)).all()) == 1
