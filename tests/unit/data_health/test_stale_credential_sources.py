"""Tests for app/services/data_health/checks/stale_credential_sources.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.models.db import CredentialSource
from app.services.data_health.checks.stale_credential_sources import StaleCredentialSourcesCheck
from tests.unit.data_health.conftest import make_source


def _check() -> StaleCredentialSourcesCheck:
    return StaleCredentialSourcesCheck()


def _old() -> datetime:
    return datetime.now(UTC) - timedelta(days=15)


def test_not_reported_for_two_weeks_is_stale(session):
    make_source(
        session,
        provider_id="github",
        account_id="me",
        source_id="s1",
        token_types_json='["api_key"]',
        last_seen=_old(),
    )

    (group,) = _check().detect(session).groups

    assert group.key == "github"
    assert group.detail == {"stale": 1, "not_a_credential": 0}


def test_recently_seen_credential_is_kept(session):
    make_source(
        session,
        provider_id="github",
        account_id="me",
        source_id="s1",
        token_types_json='["api_key"]',
    )

    assert _check().detect(session).total_count == 0


def test_fixed_provider_origin_without_token_types_is_not_a_credential(session):
    make_source(
        session,
        provider_id="github",
        account_id="me",
        source_id="s1",
        credential_origin="provider:github",
        token_types_json="[]",
    )

    (group,) = _check().detect(session).groups

    assert group.detail == {"stale": 0, "not_a_credential": 1}


def test_provider_origin_with_a_token_type_is_a_real_credential(session):
    make_source(
        session,
        provider_id="github",
        account_id="me",
        source_id="s1",
        credential_origin="provider:github",
        token_types_json='["api_key"]',
    )

    assert _check().detect(session).total_count == 0


def test_config_and_server_rows_are_never_touched(session):
    make_source(session, provider_id="github", account_id="me", source_id="config:github:me")
    make_source(
        session,
        provider_id="github",
        account_id="me",
        source_id="server:github:env",
        source_type="server",
        last_seen=_old(),
    )

    assert _check().detect(session).total_count == 0


def test_apply_deletes_only_the_reported_rows(session):
    make_source(session, provider_id="github", account_id="me", source_id="old", last_seen=_old())
    make_source(session, provider_id="github", account_id="me", source_id="fresh")
    make_source(session, provider_id="claude", account_id="x", source_id="other", last_seen=_old())

    result, _ = _check().apply(session, "github", {})

    assert result.counts == {"stale": 1, "not_a_credential": 0}
    left = {r.source_id for r in session.exec(select(CredentialSource)).all()}
    assert left == {"fresh", "other"}


def test_plan_raises_when_nothing_is_stale(session):
    with pytest.raises(ValueError, match="No stale credential sources"):
        _check().plan(session, "github", {})
