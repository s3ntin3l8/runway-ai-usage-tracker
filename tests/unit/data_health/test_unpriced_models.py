"""Tests for app/services/data_health/checks/unpriced_models.py."""

from __future__ import annotations

from sqlmodel import select

from app.models.db import UsageEvent
from app.services.data_health.checks.unpriced_models import UnpricedModelsCheck
from tests.unit.data_health.conftest import make_event, make_price


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

    group = report.groups[0]
    assert group.fixable is False
    assert group.detail["by_model"][0]["classification"] == "source_reported"


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
