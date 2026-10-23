"""Tests for app/services/data_health/checks/unpriced_claude_accounts.py."""

from __future__ import annotations

import json

import pytest
from sqlmodel import select

from app.models.db import ProviderConfig
from app.services.data_health.checks.unpriced_claude_accounts import UnpricedClaudeAccountsCheck
from tests.unit.data_health.conftest import make_config, make_latest_usage


def _check() -> UnpricedClaudeAccountsCheck:
    return UnpricedClaudeAccountsCheck()


def _quota_card(used: float = 25.0) -> str:
    return json.dumps({"unit_type": "percent", "used_value": used, "limit_value": 100.0})


def test_detect_finds_a_claude_account_nobody_priced(session):
    make_config(session, provider_id="anthropic", account_id="old@example.com")
    make_latest_usage(
        session,
        provider_id="anthropic",
        account_id="old@example.com",
        card_json=_quota_card(),
    )

    report = _check().detect(session)

    assert report.total_count == 1
    group = report.groups[0]
    assert group.key == "anthropic"
    assert group.fixable is True
    assert group.detail["priced"] == 1
    assert group.detail["skipped"] == 0


def test_detect_ignores_an_account_the_operator_priced(session):
    make_config(
        session,
        provider_id="anthropic",
        account_id="chosen@example.com",
        billing_type="pay_as_you_go",
        billing_type_source="operator",
    )

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_another_provider(session):
    make_config(session, provider_id="chatgpt", account_id="someone@example.com")

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_reports_an_account_it_cannot_price_as_not_fixable(session):
    """An account that published nothing decisive is reported, never guessed."""
    make_config(session, provider_id="anthropic", account_id="silent@example.com")

    report = _check().detect(session)

    assert report.total_count == 1
    group = report.groups[0]
    assert group.fixable is False
    assert group.not_fixable_reason
    assert group.detail["skipped"] == 1


def test_detect_counts_only_the_accounts_the_fixer_can_price(session):
    """The fixable count says what the fixer does, not what the check found."""
    make_config(session, provider_id="anthropic", account_id="old@example.com")
    make_latest_usage(
        session,
        provider_id="anthropic",
        account_id="old@example.com",
        card_json=_quota_card(),
    )
    make_config(session, provider_id="anthropic", account_id="silent@example.com")

    report = _check().detect(session)

    assert report.total_count == 2
    assert report.fixable_count == 1
    fixable = next(group for group in report.groups if group.key == "anthropic")
    assert fixable.count == 1
    assert fixable.fixable is True
    unpriceable = next(group for group in report.groups if group.key.endswith("::unpriceable"))
    assert unpriceable.count == 1
    assert unpriceable.fixable is False


def test_the_fixer_refuses_the_accounts_no_card_can_price(session):
    """The group the fixer cannot act on is not offered to it."""
    make_config(session, provider_id="anthropic", account_id="silent@example.com")

    with pytest.raises(ValueError):
        _check().plan(session, "anthropic::unpriceable", {})
    with pytest.raises(ValueError):
        _check().apply(session, "anthropic::unpriceable", {})


def test_detect_groups_by_provider(session):
    make_config(session, provider_id="anthropic", account_id="old@example.com")
    make_latest_usage(
        session,
        provider_id="anthropic",
        account_id="old@example.com",
        card_json=_quota_card(),
    )
    make_config(session, provider_id="anthropic_console", account_id="wallet@example.com")
    make_latest_usage(
        session,
        provider_id="anthropic_console",
        account_id="wallet@example.com",
        card_json=json.dumps({"unit_type": "currency", "currency": "USD"}),
    )

    report = _check().detect(session)

    assert {group.key for group in report.groups} == {"anthropic", "anthropic_console"}
    assert report.total_count == 2


def test_plan_is_read_only(session):
    make_config(session, provider_id="anthropic", account_id="old@example.com")
    make_latest_usage(
        session,
        provider_id="anthropic",
        account_id="old@example.com",
        card_json=_quota_card(),
    )

    _check().plan(session, "anthropic", {})

    row = session.exec(select(ProviderConfig)).one()
    assert row.billing_type == "unknown"
    assert row.billing_type_source is None


def test_apply_prices_the_account_and_clears_the_finding(session):
    make_config(session, provider_id="anthropic", account_id="old@example.com")
    make_latest_usage(
        session,
        provider_id="anthropic",
        account_id="old@example.com",
        card_json=_quota_card(),
    )

    result, hooks = _check().apply(session, "anthropic", {})

    assert result.counts["priced"] == 1
    assert hooks == []
    row = session.exec(select(ProviderConfig)).one()
    assert row.billing_type == "subscription"
    assert row.billing_type_source == "detected"
    assert _check().detect(session).total_count == 0


def test_apply_never_touches_the_operators_own_choice(session):
    make_config(
        session,
        provider_id="anthropic",
        account_id="chosen@example.com",
        billing_type="pay_as_you_go",
        billing_type_source="operator",
    )
    make_latest_usage(
        session,
        provider_id="anthropic",
        account_id="chosen@example.com",
        card_json=_quota_card(),
    )

    result, _hooks = _check().apply(session, "anthropic", {})

    assert result.counts["priced"] == 0
    row = session.exec(select(ProviderConfig)).one()
    assert row.billing_type == "pay_as_you_go"
    assert row.billing_type_source == "operator"
