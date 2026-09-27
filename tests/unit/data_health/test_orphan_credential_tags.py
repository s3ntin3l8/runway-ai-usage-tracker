"""Tests for app/services/data_health/checks/orphan_credential_tags.py."""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.models.db import CredentialTag
from app.services.data_health.checks.orphan_credential_tags import OrphanCredentialTagsCheck
from tests.unit.data_health.conftest import make_config, make_tag


def _check() -> OrphanCredentialTagsCheck:
    return OrphanCredentialTagsCheck()


def test_detect_finds_a_tag_pointing_at_default_with_no_default_config(session):
    make_tag(session, provider_id="minimax", credential_origin="path:/x", account_id="default")

    report = _check().detect(session)

    assert report.total_count == 1
    assert report.groups[0].key == "minimax"


def test_detect_ignores_a_tag_when_a_real_default_config_exists(session):
    make_config(session, provider_id="minimax", account_id="default")
    make_tag(session, provider_id="minimax", credential_origin="path:/x", account_id="default")

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_a_tag_pointing_at_a_real_account(session):
    make_tag(
        session, provider_id="minimax", credential_origin="path:/x", account_id="alice@example.com"
    )

    report = _check().detect(session)

    assert report.total_count == 0


def test_plan_is_read_only(session):
    make_tag(session, provider_id="minimax", credential_origin="path:/x", account_id="default")

    _check().plan(session, "minimax", {"action": "delete"})

    tag = session.exec(select(CredentialTag)).one()
    assert tag.account_id == "default"


def test_apply_delete_removes_the_orphan_tag(session):
    make_tag(session, provider_id="minimax", credential_origin="path:/x", account_id="default")

    result, hooks = _check().apply(session, "minimax", {"action": "delete"})

    assert result.counts["tags_deleted"] == 1
    assert hooks == []
    assert list(session.exec(select(CredentialTag))) == []
    assert _check().detect(session).total_count == 0


def test_apply_repoint_moves_the_tag_onto_a_configured_account(session):
    make_config(session, provider_id="minimax", account_id="alice@example.com")
    make_tag(session, provider_id="minimax", credential_origin="path:/x", account_id="default")

    result, _hooks = _check().apply(
        session, "minimax", {"action": "repoint", "target": "alice@example.com"}
    )

    assert result.counts["tags_repointed"] == 1
    tag = session.exec(select(CredentialTag)).one()
    assert tag.account_id == "alice@example.com"


def test_apply_repoint_without_a_valid_target_raises(session):
    make_tag(session, provider_id="minimax", credential_origin="path:/x", account_id="default")

    with pytest.raises(ValueError, match="repoint requires a target"):
        _check().apply(session, "minimax", {"action": "repoint"})


def test_apply_unknown_action_raises(session):
    make_tag(session, provider_id="minimax", credential_origin="path:/x", account_id="default")

    with pytest.raises(ValueError, match="unknown action"):
        _check().apply(session, "minimax", {"action": "bogus"})
