"""Tests for app/services/data_health/checks/stale_priced_models.py and the
`estimate_only` mode of app/services/maintenance/recost.py behind it."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlmodel import select

from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.data_health.checks.stale_priced_models import StalePricedModelsCheck
from app.services.maintenance.recost import apply_recost, plan_recost
from tests.unit.data_health.conftest import make_config, make_event, make_price

_CHECK = StalePricedModelsCheck()
_OLD = datetime(2026, 3, 1, tzinfo=UTC)
_NEW = datetime(2026, 9, 1, tzinfo=UTC)


def _ev(session, event_id, *, estimated, cost=None, **overrides) -> int:
    """An anthropic message whose stored estimate (and, by default, displayed
    cost) is `estimated`."""
    base = {
        "provider_id": "anthropic",
        "account_id": "acct",
        "model_id": "sonnet-5.5",
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_cache_read": 1_000_000,
        "cost_estimated_usd": estimated,
        "cost_usd": estimated if cost is None else cost,
        "ts": _NEW,
    }
    base.update(overrides)
    return make_event(session, event_id=event_id, **base).id  # type: ignore[return-value]


def _reload(session, event_id: int) -> UsageEvent:
    session.expire_all()
    return session.get(UsageEvent, event_id)  # type: ignore[return-value]


def _groups(session) -> dict[str, object]:
    return {g.key: g for g in _CHECK.detect(session).groups}


def test_a_real_price_change_is_not_applied_to_older_events(session):
    make_price(
        session, provider_id="anthropic", model_id="sonnet-5.5", rate=1.0, cache_read_per_mtok=1.0
    )
    make_price(
        session,
        provider_id="anthropic",
        model_id="sonnet-5.5",
        rate=2.0,
        cache_read_per_mtok=2.0,
        effective_from=date(2026, 6, 1),
    )
    old = _ev(session, "old", estimated=1.0, ts=_OLD)
    new = _ev(session, "new", estimated=2.0, ts=_NEW)

    assert _CHECK.detect(session).groups == []
    plan = plan_recost(session, ["anthropic"], estimate_only=True)
    assert plan.updated == 0
    assert apply_recost(session, ["anthropic"], estimate_only=True).updated == 0
    assert _reload(session, old).cost_estimated_usd == 1.0
    assert _reload(session, new).cost_estimated_usd == 2.0


def test_a_stale_event_before_a_price_change_is_repriced_at_its_own_older_rate(session):
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=1.0)
    make_price(
        session,
        provider_id="anthropic",
        model_id="sonnet-5.5",
        cache_read_per_mtok=2.0,
        effective_from=date(2026, 6, 1),
    )
    old = _ev(session, "old", estimated=7.0, ts=_OLD)  # wrong, but dated before the new row
    new = _ev(session, "new", estimated=7.0, ts=_NEW)

    apply_recost(session, ["anthropic"], estimate_only=True)

    assert _reload(session, old).cost_estimated_usd == pytest.approx(1.0)  # not 2.0
    assert _reload(session, new).cost_estimated_usd == pytest.approx(2.0)


def test_in_place_cache_read_correction_is_flagged_and_fixed_for_that_model_only(session):
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=0.05)
    make_price(session, provider_id="anthropic", model_id="opus-5", cache_read_per_mtok=0.5)
    stale = _ev(session, "sonnet", estimated=0.20)
    other_stale = _ev(session, "opus", estimated=9.0, model_id="opus-5")  # stale too, other model

    groups = _groups(session)
    assert set(groups) == {"anthropic::sonnet-5.5", "anthropic::opus-5"}
    sonnet = groups["anthropic::sonnet-5.5"]
    assert sonnet.fixable and sonnet.count == 1
    assert sonnet.samples[0].detail["delta_usd"] == pytest.approx(-0.15)

    result = _CHECK.apply(session, "anthropic::sonnet-5.5", {})[0]

    assert result.counts["updated"] == 1
    fixed = _reload(session, stale)
    assert fixed.cost_estimated_usd == pytest.approx(0.05)
    assert fixed.cost_usd == pytest.approx(0.05)
    assert fixed.cost_cache_read == pytest.approx(0.05)
    untouched = _reload(session, other_stale)
    assert untouched.cost_estimated_usd == 9.0 and untouched.cost_usd == 9.0
    assert set(_groups(session)) == {"anthropic::opus-5"}


def test_new_model_row_fixes_events_priced_through_the_family_fallback(session):
    make_price(session, provider_id="anthropic", model_id="haiku", cache_read_per_mtok=0.1)
    make_price(session, provider_id="anthropic", model_id="haiku-5.5", cache_read_per_mtok=0.01)
    ev = _ev(session, "h", estimated=0.1, model_id="haiku-5.5")

    assert set(_groups(session)) == {"anthropic::haiku-5.5"}
    _CHECK.apply(session, "anthropic::haiku-5.5", {})

    assert _reload(session, ev).cost_estimated_usd == pytest.approx(0.01)
    assert _groups(session) == {}


def test_a_reported_cost_keeps_cost_usd_and_cost_reported_usd(session):
    make_config(session, provider_id="anthropic", account_id="acct", billing_type="pay_as_you_go")
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=0.05)
    ev = _ev(session, "payg", estimated=0.20, cost=0.77, cost_reported_usd=0.77)

    plan = _CHECK.plan(session, "anthropic::sonnet-5.5", {})
    assert plan.counts["updated"] == 1 and plan.counts["events_keeping_cost_usd"] == 1
    _CHECK.apply(session, "anthropic::sonnet-5.5", {})

    fixed = _reload(session, ev)
    assert fixed.cost_estimated_usd == pytest.approx(0.05)
    assert fixed.cost_usd == 0.77
    assert fixed.cost_reported_usd == 0.77


def test_a_billing_type_change_since_ingest_keeps_cost_usd(session):
    # Ingested as pay-as-you-go (cost_usd is the reported amount), the account
    # is a subscription now: a full recost would rewrite cost_usd to the estimate.
    make_config(session, provider_id="anthropic", account_id="acct", billing_type="subscription")
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=0.05)
    ev = _ev(session, "bt", estimated=0.20, cost=0.33, cost_reported_usd=0.33)

    full = plan_recost(session, ["anthropic"])
    assert full.updated == 1  # the full recost would move cost_usd
    apply_recost(session, ["anthropic"], estimate_only=True)

    fixed = _reload(session, ev)
    assert fixed.cost_usd == 0.33 and fixed.cost_reported_usd == 0.33
    assert fixed.cost_estimated_usd == pytest.approx(0.05)


def test_long_tier_boundary_and_cloud_aggregate_price_correctly(session):
    make_price(
        session,
        provider_id="anthropic",
        model_id="haiku-5.5",
        rate=1.0,
        long_prompt_threshold_tokens=100_000,
        long_input_per_mtok=2.0,
        long_output_per_mtok=2.0,
    )
    common = {"model_id": "haiku-5.5", "tokens_cache_read": 0}
    under = _ev(session, "under", estimated=9.9, tokens_input=100_000, **common)
    over = _ev(session, "over", estimated=9.9, tokens_input=100_001, **common)
    cloud = make_event(
        session,
        event_id="x",
        provider_id="anthropic",
        account_id="acct",
        model_id="haiku-5.5",
        tokens_input=500_000,
        tokens_output=0,
        cost_estimated_usd=9.9,
        cost_usd=9.9,
        ts=_NEW,
    )
    cloud.event_id = "cloud:abc:r:1"
    session.add(cloud)
    session.commit()
    cloud_id = cloud.id

    group = _groups(session)["anthropic::haiku-5.5"]
    assert group.count == 3
    plan = _CHECK.plan(session, "anthropic::haiku-5.5", {})
    assert plan.counts["updated"] == 3
    assert plan.counts["new_estimate_usd"] == pytest.approx(0.8, abs=1e-3)

    _CHECK.apply(session, "anthropic::haiku-5.5", {})

    assert _reload(session, under).cost_estimated_usd == pytest.approx(0.1)
    assert _reload(session, over).cost_estimated_usd == pytest.approx(0.200002)
    assert _reload(session, cloud_id).cost_estimated_usd == pytest.approx(0.5)  # standard tier
    assert _groups(session) == {}


def test_apply_is_idempotent_and_leaves_the_detect_clean(session):
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=0.05)
    _ev(session, "a", estimated=0.20)

    first = _CHECK.apply(session, "anthropic::sonnet-5.5", {})[0]
    second = _CHECK.apply(session, "anthropic::sonnet-5.5", {})[0]

    assert first.counts["updated"] == 1
    assert second.counts["updated"] == 0
    assert second.counts["rollups_rebuilt_pairs"] == 0
    assert _CHECK.detect(session).total_count == 0


def test_detect_hint_agrees_with_the_exact_plan_on_a_mixed_fixture(session):
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=0.05)
    make_price(session, provider_id="anthropic", model_id="opus-5", cache_read_per_mtok=0.5)
    make_price(session, provider_id="anthropic", model_id="haiku", cache_read_per_mtok=0.1)
    make_price(
        session,
        provider_id="anthropic",
        model_id="haiku",
        cache_read_per_mtok=0.2,
        effective_from=date(2026, 6, 1),
    )
    for i in range(3):
        _ev(session, f"s{i}", estimated=0.20)  # stale
        _ev(session, f"o{i}", estimated=0.5, model_id="opus-5")  # current
    _ev(session, "h-old", estimated=0.1, model_id="haiku", ts=_OLD)  # current at its date
    _ev(session, "h-new", estimated=0.1, model_id="haiku", ts=_NEW)  # stale (0.2 now)
    _ev(session, "free", estimated=0.0, cost=0.0)  # $0 belongs to unpriced_models

    groups = _groups(session)
    assert set(groups) == {"anthropic::sonnet-5.5", "anthropic::haiku"}
    for key, group in groups.items():
        provider_id, model_id = key.split("::")
        exact = plan_recost(
            session, [provider_id], estimate_only=True, models=[model_id], sample_size=0
        )
        assert exact.updated == group.count
        assert exact.new_estimate_sum - exact.stored_estimate_sum == pytest.approx(
            group.samples[0].detail["delta_usd"], abs=1e-4
        )
    for model_id in ("opus-5",):
        assert (
            plan_recost(session, ["anthropic"], estimate_only=True, models=[model_id]).updated == 0
        )


def test_apply_rebuilds_rollups_for_the_affected_pairs_only(session):
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=0.05)
    _ev(session, "a", estimated=0.20, account_id="stale-acct")
    _ev(session, "b", estimated=0.05, account_id="clean-acct")

    result = _CHECK.apply(session, "anthropic::sonnet-5.5", {})[0]

    assert result.counts["rollups_rebuilt_pairs"] == 1
    rollups = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).all()
    assert {r.account_id: round(r.cost_usd, 4) for r in rollups} == {"stale-acct": 0.05}


def test_plan_is_read_only_and_apply_modes_are_exclusive(session):
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=0.05)
    ev = _ev(session, "a", estimated=0.20)

    plan = _CHECK.plan(session, "anthropic::sonnet-5.5", {})

    assert plan.counts["delta_usd"] == pytest.approx(-0.15)
    assert plan.confirmation_text and "sonnet-5.5" in plan.confirmation_text
    assert _reload(session, ev).cost_estimated_usd == 0.20
    with pytest.raises(ValueError, match="mutually exclusive"):
        plan_recost(session, ["anthropic"], only_zero_cost=True, estimate_only=True)
    with pytest.raises(ValueError, match="malformed"):
        _CHECK.plan(session, "anthropic", {})


def test_unpriced_free_and_zero_cost_events_are_not_this_checks(session):
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=0.05)
    make_price(session, provider_id="openrouter", model_id="vendor/model:free", rate=5.0)
    _ev(session, "zero", estimated=0.0, cost=0.0)  # unpriced_models' scope
    _ev(session, "nomodelrow", estimated=3.0, model_id="never-seeded")  # no rate resolves
    _ev(session, "free", estimated=1.0, provider_id="openrouter", model_id="vendor/model:free")

    assert _CHECK.detect(session).groups == []
    exact = plan_recost(session, None, estimate_only=True)
    assert exact.updated == 0 and exact.zeroed == 0


def test_detect_issues_a_constant_number_of_queries(session, query_counter):
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=0.05)
    for i in range(30):
        _ev(session, f"e{i}", estimated=0.20, model_id="sonnet-5.5")
        _ev(session, f"f{i}", estimated=0.20, model_id=f"m-{i}")

    query_counter.reset()
    _CHECK.detect(session)

    assert query_counter.count == 2  # pricing preload + one aggregate


def test_an_estimate_that_drops_to_zero_is_reported_as_zeroed_not_updated(session):
    make_price(session, provider_id="anthropic", model_id="sonnet-5.5", cache_read_per_mtok=0.0)
    ev = _ev(session, "z", estimated=0.20)

    plan = _CHECK.plan(session, "anthropic::sonnet-5.5", {})

    assert plan.counts["zeroed"] == 1 and plan.counts["updated"] == 0
    result = _CHECK.apply(session, "anthropic::sonnet-5.5", {})[0]
    assert result.counts["zeroed"] == 1
    assert _reload(session, ev).cost_usd == 0.0
