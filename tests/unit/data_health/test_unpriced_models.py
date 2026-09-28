"""Tests for app/services/data_health/checks/unpriced_models.py."""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.models.db import UsageEvent
from app.services.data_health.checks.unpriced_models import UnpricedModelsCheck
from tests.unit.data_health.conftest import make_config, make_event, make_price


def _check() -> UnpricedModelsCheck:
    return UnpricedModelsCheck()


def test_detect_classifies_a_priced_zero_cost_event_as_recost_fixes_it(session):
    make_event(session, event_id="1", provider_id="chatgpt", model_id="gpt-6-sol", cost_usd=0.0)
    make_price(session, provider_id="chatgpt", model_id="gpt-6-sol", rate=2.0)

    report = _check().detect(session)

    assert report.total_count == 1
    group = report.groups[0]
    assert group.fixable is True
    assert group.detail["by_model"][0]["classification"] == "recost_fixes_it"


def test_detect_preloads_pricing_once_for_many_models(session, query_counter):
    for i in range(12):
        make_event(
            session,
            event_id=f"unpriced-{i}",
            provider_id="chatgpt",
            model_id=f"unpriced-model-{i}",
            cost_usd=0.0,
        )

    query_counter.reset()
    _check().detect(session)

    pricing_queries = [
        statement
        for statement in query_counter.statements
        if "provider_pricing" in statement.lower()
    ]
    assert len(pricing_queries) == 1


def test_payg_reported_cost_overrides_a_zero_rate_and_is_recostable(session):
    make_config(session, provider_id="chatgpt", account_id="acct", billing_type="pay_as_you_go")
    make_event(
        session,
        event_id="payg-reported",
        provider_id="chatgpt",
        account_id="acct",
        model_id="gpt-zero-rate",
        cost_usd=0.0,
        cost_reported_usd=0.25,
        tokens_input=1_000,
        tokens_output=0,
    )
    make_price(session, provider_id="chatgpt", model_id="gpt-zero-rate", rate=0.0)

    report = _check().detect(session)

    assert report.groups[0].fixable is True
    model = report.groups[0].detail["by_model"][0]
    assert model["classification"] == "recost_fixes_it"
    assert model["recost_fixes_it"] == 1
    assert model["verified_zero"] == 0


def test_detect_classifies_an_unseeded_model_as_needs_seed_row(session):
    make_event(session, event_id="1", provider_id="chatgpt", model_id="gpt-6-luna", cost_usd=0.0)

    report = _check().detect(session)

    group = report.groups[0]
    assert group.fixable is False
    assert group.detail["by_model"][0]["classification"] == "needs_seed_row"
    assert "PRICING_SEED" in group.not_fixable_reason


def test_detect_classifies_a_reported_zero_cost_as_source_reported(session):
    make_event(
        session,
        event_id="1",
        provider_id="chatgpt",
        model_id="gpt-6-luna",
        cost_usd=0.0,
        cost_reported_usd=0.0,
    )

    report = _check().detect(session)

    # Reported $0 is evidence from the source, not independent proof; it is
    # visible as informational, not raised as a warning.
    assert report.severity.value == "info"
    assert report.groups[0].detail["by_model"][0]["classification"] == "source_reported"


def test_detect_reports_a_configured_zero_rate_as_informational(session):
    make_event(session, event_id="zero", provider_id="chatgpt", model_id="gpt-free", cost_usd=0.0)
    make_price(session, provider_id="chatgpt", model_id="gpt-free", rate=0.0)

    report = _check().detect(session)

    assert report.severity.value == "info"
    assert report.groups[0].detail["by_model"][0]["classification"] == "verified_zero"


def test_informational_group_rejects_preview_and_apply(session):
    make_event(session, event_id="zero", provider_id="chatgpt", model_id="gpt-free", cost_usd=0.0)
    make_price(session, provider_id="chatgpt", model_id="gpt-free", rate=0.0)
    group_key = _check().detect(session).groups[0].key

    with pytest.raises(ValueError, match="informational zero-cost"):
        _check().plan(session, group_key, {})
    with pytest.raises(ValueError, match="informational zero-cost"):
        _check().apply(session, group_key, {})


def test_mixed_model_evidence_is_split_between_actionable_and_info_groups(session):
    make_config(session, provider_id="chatgpt", account_id="reported", billing_type="pay_as_you_go")
    make_event(
        session,
        event_id="priced-zero",
        provider_id="chatgpt",
        account_id="metered",
        model_id="gpt-6-sol",
        cost_usd=0.0,
    )
    make_event(
        session,
        event_id="source-reported-zero",
        provider_id="chatgpt",
        account_id="reported",
        model_id="gpt-6-sol",
        cost_usd=0.0,
        cost_reported_usd=0.0,
    )
    make_price(session, provider_id="chatgpt", model_id="gpt-6-sol", rate=2.0)

    report = _check().detect(session)

    actionable, informational = report.groups
    actionable_model = actionable.detail["by_model"][0]
    informational_model = informational.detail["by_model"][0]
    assert actionable_model["count"] == 1
    assert actionable_model["recost_fixes_it"] == 1
    assert actionable_model["source_reported"] == 0
    assert informational_model["count"] == 1
    assert informational_model["source_reported"] == 1
    assert informational_model["recost_fixes_it"] == 0


def test_source_reported_zero_with_positive_estimate_is_not_called_verified(session):
    make_config(session, provider_id="chatgpt", account_id="acct", billing_type="pay_as_you_go")
    make_event(
        session,
        event_id="reported-zero",
        provider_id="chatgpt",
        account_id="acct",
        model_id="gpt-6-sol",
        cost_usd=0.0,
        cost_reported_usd=0.0,
        tokens_input=1_000_000,
        tokens_output=0,
    )
    make_price(session, provider_id="chatgpt", model_id="gpt-6-sol", rate=2.0)

    report = _check().detect(session)

    assert report.severity.value == "info"
    model = report.groups[0].detail["by_model"][0]
    assert model["classification"] == "source_reported"
    assert model["source_reported"] == 1


def test_detect_classifies_source_reported_despite_extra_zero_token_events(session):
    """Regression for #375: the old per-group `reported` count query had no
    token-bearing filter (unlike the main grouped query, which requires
    tokens_input+tokens_output+tokens_cache_read+tokens_cache_create > 0).
    A group with 2 token-bearing, cost_reported_usd-backed events (count=2)
    plus a zero-token event that ALSO carries cost_reported_usd used to
    inflate the old unfiltered `reported` count to 3 — 3 != 2, so `_classify`
    fell through to `needs_seed_row` even though every token-bearing event
    in the group *is* cost_reported_usd-backed, contradicting the check's
    own docstring. Fixed by computing `reported` in the same token-bearing
    grouped query as `count`, so the zero-token event (reported or not) no
    longer affects the comparison.
    """
    make_event(
        session,
        event_id="1",
        provider_id="chatgpt",
        model_id="gpt-6-luna",
        cost_usd=0.0,
        cost_reported_usd=0.0,
    )
    make_event(
        session,
        event_id="2",
        provider_id="chatgpt",
        model_id="gpt-6-luna",
        cost_usd=0.0,
        cost_reported_usd=0.0,
    )
    # A zero-token, cost_reported_usd-backed event for the same
    # (provider, model) — excluded from the main grouped query's
    # token-bearing filter, so it must not affect the `reported == count`
    # comparison either. This is the event that inflated the old,
    # unfiltered `reported` count past `count` and triggered the bug.
    make_event(
        session,
        event_id="3",
        provider_id="chatgpt",
        model_id="gpt-6-luna",
        cost_usd=0.0,
        cost_reported_usd=0.0,
        tokens_input=0,
        tokens_output=0,
    )
    # A fourth, zero-token event with no cost_reported_usd at all — covers
    # the "with or without cost_reported_usd" half of the zero-token case.
    make_event(
        session,
        event_id="4",
        provider_id="chatgpt",
        model_id="gpt-6-luna",
        cost_usd=0.0,
        tokens_input=0,
        tokens_output=0,
    )

    report = _check().detect(session)

    group = report.groups[0]
    assert group.detail["by_model"][0]["classification"] == "source_reported"
    assert group.fixable is False


def test_detect_excludes_free_suffixed_models(session):
    make_event(session, event_id="1", provider_id="opencode", model_id="grok:free", cost_usd=0.0)

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_excludes_opencode_free_provider(session):
    make_event(
        session, event_id="1", provider_id="opencode-free", model_id="anything", cost_usd=0.0
    )

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_excludes_events_with_no_tokens(session):
    make_event(
        session,
        event_id="1",
        provider_id="chatgpt",
        model_id="gpt-6-sol",
        cost_usd=0.0,
        tokens_input=0,
        tokens_output=0,
    )

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_includes_events_with_reasoning_tokens_only(session):
    make_event(
        session,
        event_id="reasoning",
        provider_id="chatgpt",
        model_id="gpt-6-sol",
        cost_usd=0.0,
        tokens_input=0,
        tokens_output=0,
        tokens_reasoning=100,
    )

    report = _check().detect(session)

    assert report.total_count == 1
    assert report.groups[0].fixable is False


def test_detect_excludes_events_already_priced_nonzero(session):
    make_event(session, event_id="1", provider_id="chatgpt", model_id="gpt-6-sol", cost_usd=1.0)

    report = _check().detect(session)

    assert report.total_count == 0


def test_plan_is_read_only(session):
    make_event(session, event_id="1", provider_id="chatgpt", model_id="gpt-6-sol", cost_usd=0.0)
    make_price(session, provider_id="chatgpt", model_id="gpt-6-sol", rate=2.0)

    _check().plan(session, "chatgpt", {})

    ev = session.exec(select(UsageEvent)).one()
    assert ev.cost_usd == 0.0


def test_apply_recosts_the_provider_and_clears_the_finding(session):
    make_event(
        session,
        event_id="1",
        provider_id="chatgpt",
        model_id="gpt-6-sol",
        cost_usd=0.0,
        tokens_input=1_000_000,
        tokens_output=0,
    )
    make_price(session, provider_id="chatgpt", model_id="gpt-6-sol", rate=2.0)

    result, hooks = _check().apply(session, "chatgpt", {})

    assert result.counts["updated"] == 1
    assert hooks == []
    ev = session.exec(select(UsageEvent)).one()
    assert ev.cost_usd == 2.0  # 1M tokens @ $2/Mtok
    assert _check().detect(session).total_count == 0


def test_apply_never_touches_a_nonzero_cost_event(session):
    """Guard from the plan: unpriced_models' fix must never lower an
    already-nonzero cost, even for a different model under the same
    provider that happens to also resolve a (cheaper) price row."""
    make_event(
        session, event_id="reported", provider_id="chatgpt", model_id="gpt-6-luna", cost_usd=50.0
    )
    make_event(
        session, event_id="unpriced", provider_id="chatgpt", model_id="gpt-6-sol", cost_usd=0.0
    )
    make_price(session, provider_id="chatgpt", model_id="gpt-6-sol", rate=2.0)
    make_price(session, provider_id="chatgpt", model_id="gpt-6-luna", rate=2.0)

    _check().apply(session, "chatgpt", {})

    reported = session.exec(select(UsageEvent).where(UsageEvent.event_id == "msg_reported")).one()
    assert reported.cost_usd == 50.0  # untouched
