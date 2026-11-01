"""Tests for app/services/data_health/checks/unpriced_models.py."""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.models.db import UsageEvent
from app.services.data_health.checks.unpriced_models import UnpricedModelsCheck
from app.services.maintenance.recost import apply_recost, plan_recost
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
    make_event(
        session, event_id="zero", provider_id="chatgpt", model_id="gpt-zero-rate", cost_usd=0.0
    )
    make_price(session, provider_id="chatgpt", model_id="gpt-zero-rate", rate=0.0)

    report = _check().detect(session)

    assert report.severity.value == "info"
    assert report.groups[0].detail["by_model"][0]["classification"] == "verified_zero"


def test_informational_group_rejects_preview_and_apply(session):
    make_event(
        session, event_id="zero", provider_id="chatgpt", model_id="gpt-zero-rate", cost_usd=0.0
    )
    make_price(session, provider_id="chatgpt", model_id="gpt-zero-rate", rate=0.0)
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
    make_event(
        session,
        event_id="2",
        provider_id="opencode",
        model_id="deepseek-v4-flash-free",
        cost_usd=0.0,
    )
    make_event(
        session, event_id="3", provider_id="opencode", model_id="openrouter/free", cost_usd=0.0
    )
    make_event(session, event_id="4", provider_id="opencode", model_id="unknown", cost_usd=0.0)
    make_event(
        session,
        event_id="5",
        provider_id="opencode-zen",
        model_id="deepseek-v4-flash-free",
        cost_usd=0.0,
    )
    make_event(
        session,
        event_id="6",
        provider_id="opencode-zen",
        model_id="mimo-v2.5-free",
        cost_usd=0.0,
    )
    make_event(
        session,
        event_id="7",
        provider_id="opencode-byok",
        model_id="gemini-2.5-flash-free",
        cost_usd=0.0,
    )
    make_event(
        session,
        event_id="8",
        provider_id="hermes-auto",
        model_id="deepseek-v4-flash-free",
        cost_usd=0.0,
    )

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_excludes_free_suffixed_models_for_every_provider(session):
    """Free-tier ids are excluded for every provider, not just the OpenCode
    ones — the cost calculator's segment-trim fallback is blocked for them
    too, so a "-free" id can never reach a paid sibling row and be billed at
    list price."""
    make_event(
        session,
        event_id="1",
        provider_id="minimax",
        model_id="MiniMax-M3.1-Flash-free",
        cost_usd=0.0,
    )
    make_event(
        session,
        event_id="2",
        provider_id="minimax",
        model_id="minimax-m3.1-flash-free",
        cost_usd=0.0,
    )
    make_event(
        session,
        event_id="3",
        provider_id="openrouter",
        model_id="vendor/model:free",
        cost_usd=0.0,
    )
    make_event(
        session,
        event_id="4",
        provider_id="minimax",
        model_id="MiniMax-M3-free",
        cost_usd=0.0,
    )

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_excludes_opencode_free_provider(session):
    make_event(
        session, event_id="1", provider_id="opencode-free", model_id="anything", cost_usd=0.0
    )

    report = _check().detect(session)
    assert report.total_count == 0


def test_detect_flags_unpriced_ollama_cloud_model(session):
    """OpenCode's ollama-cloud events fold onto canonical 'ollama' and carry plain model IDs
    (e.g. 'nemotron-3-ultra'); they must be flagged as needs_seed_row."""
    make_event(
        session, event_id="1", provider_id="ollama", model_id="nemotron-3-ultra", cost_usd=0.0
    )

    report = _check().detect(session)
    assert report.total_count == 1
    assert report.groups[0].detail["by_model"][0]["classification"] == "needs_seed_row"


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


def _opencode_zero_cost_event(session, event_id: str, account_id: str = "acct") -> None:
    """A zero-cost opencode event with no reported cost of its own."""
    make_event(
        session,
        event_id=event_id,
        provider_id="opencode",
        account_id=account_id,
        model_id="gpt-6-sol",
        cost_usd=0.0,
        cost_reported_usd=None,
        tokens_input=1_000_000,
        tokens_output=0,
    )
    make_price(session, provider_id="opencode", model_id="gpt-6-sol", rate=2.0)


def test_payg_opencode_event_is_not_fixable_and_recost_agrees(session):
    """#1125: the check and the recost plan must agree.

    On a pay-as-you-go opencode account the legacy report (the stored cost) is
    $0 and the billing type trusts that report over the price estimate, so
    recost computes $0 again. The check must not call such an event fixable
    while recost skips it — the two would disagree about the same event.
    """
    make_config(session, provider_id="opencode", account_id="acct", billing_type="pay_as_you_go")
    _opencode_zero_cost_event(session, "payg")

    report = _check().detect(session)

    group = report.groups[0]
    assert group.fixable is False
    model = group.detail["by_model"][0]
    assert model["classification"] == "needs_seed_row"
    assert model["needs_seed_row"] == 1
    assert model["recost_fixes_it"] == 0

    plan = plan_recost(session, ["opencode"], only_zero_cost=True)
    assert plan.updated == 0
    assert plan.skipped_still_unpriced == 1


def test_subscription_opencode_event_is_still_recosted(session):
    """#1125: the existing subscription case must keep working.

    A subscription account has no report to trust, so the price estimate
    applies — the check classifies the event as fixable and recost reprices it.
    """
    make_config(session, provider_id="opencode", account_id="acct", billing_type="subscription")
    _opencode_zero_cost_event(session, "sub")

    report = _check().detect(session)

    group = report.groups[0]
    assert group.fixable is True
    assert group.detail["by_model"][0]["classification"] == "recost_fixes_it"

    result = apply_recost(session, ["opencode"], only_zero_cost=True)
    assert result.updated == 1
    ev = session.exec(select(UsageEvent)).one()
    assert ev.cost_usd == pytest.approx(2.0)


def test_reason_does_not_advise_a_seed_row_when_a_price_row_already_resolves(session):
    """#1125: the group reason must not send the operator down a dead end.

    A price row resolves for this model; the cost stays $0 because the
    pay-as-you-go account trusts the source's $0 over the estimate. Adding a
    PRICING_SEED row would change nothing, so the reason must say so instead
    of asking for one.
    """
    make_config(session, provider_id="opencode", account_id="acct", billing_type="pay_as_you_go")
    _opencode_zero_cost_event(session, "payg")

    group = _check().detect(session).groups[0]

    assert group.fixable is False
    assert "PRICING_SEED" not in group.not_fixable_reason
    assert "pay-as-you-go" in group.not_fixable_reason


def test_reason_still_advises_a_seed_row_for_a_model_with_no_rate(session):
    """#1125: the unseeded case keeps its advice — a rate really is missing."""
    make_event(
        session,
        event_id="unseeded",
        provider_id="chatgpt",
        model_id="gpt-6-luna",
        cost_usd=0.0,
    )

    group = _check().detect(session).groups[0]

    assert group.fixable is False
    assert "PRICING_SEED" in group.not_fixable_reason


def test_the_payload_keys_are_stable(session):
    """#1125: the per-model payload must not grow a counter.

    The memory benchmark digests `_classify_events`'s output and the webapp
    reads these keys, so a counter leaking into the mapping would change
    both. `seed_proof` stays an attribute of the bucket, not a key.
    """
    make_config(session, provider_id="opencode", account_id="acct", billing_type="pay_as_you_go")
    _opencode_zero_cost_event(session, "payg")

    model = _check().detect(session).groups[0].detail["by_model"][0]

    assert sorted(model) == [
        "classification",
        "count",
        "model_id",
        "needs_seed_row",
        "recost_fixes_it",
        "source_reported",
        "verified_zero",
    ]


def test_reason_explains_a_payg_zero_report_even_without_a_price_row(session):
    """#1125: a seed row cannot help a pay-as-you-go account that reported $0.

    The report wins over any estimate, so the cost stays $0 whether or not a
    price row exists. The reason must say that instead of asking for a seed
    row — adding one leaves the event at $0.
    """
    make_config(session, provider_id="opencode", account_id="acct", billing_type="pay_as_you_go")
    # A model with no price row at all: the reported $0 still decides.
    make_event(
        session,
        event_id="payg",
        provider_id="opencode",
        account_id="acct",
        model_id="gpt-6-luna",
        cost_usd=0.0,
        cost_reported_usd=None,
        tokens_input=1_000_000,
    )

    group = _check().detect(session).groups[0]

    assert group.fixable is False
    assert "PRICING_SEED" not in group.not_fixable_reason
    assert "pay-as-you-go" in group.not_fixable_reason

    # Seeding a rate does not rescue it either — the report still wins.
    make_price(session, provider_id="opencode", model_id="gpt-6-luna", rate=2.0)
    plan = plan_recost(session, ["opencode"], only_zero_cost=True)
    assert plan.updated == 0
    assert plan.skipped_still_unpriced == 1


def test_reason_covers_both_kinds_of_zero_cost_event_in_one_group(session):
    """#1125: a provider can hold both kinds at once, and the reason must
    cover both — the events a seed row would rescue and the ones it would not."""
    make_config(session, provider_id="opencode", account_id="sub", billing_type="subscription")
    make_config(session, provider_id="opencode", account_id="payg", billing_type="pay_as_you_go")
    # A subscription account has no report to trust, so a seed row rescues it.
    make_event(
        session,
        event_id="unseeded",
        provider_id="opencode",
        account_id="sub",
        model_id="gpt-6-luna",
        cost_usd=0.0,
        cost_reported_usd=None,
        tokens_input=1_000_000,
    )
    # A pay-as-you-go account that reported $0 keeps it at $0 either way.
    make_event(
        session,
        event_id="payg",
        provider_id="opencode",
        account_id="payg",
        model_id="gpt-6-sol",
        cost_usd=0.0,
        cost_reported_usd=None,
        tokens_input=1_000_000,
    )

    group = _check().detect(session).groups[0]

    assert group.fixable is False
    reason = group.not_fixable_reason
    assert "PRICING_SEED" in reason
    assert "pay-as-you-go" in reason
