"""Tests for the `fallback_priced_events` check and its maintenance module."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import scripts.reprice_fallback_priced_events as script
from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.cost_calculator import PricingIndex, resolve_price_row
from app.services.data_health.checks.fallback_priced_events import FallbackPricedEventsCheck
from app.services.maintenance.fallback_priced_events import (
    FallbackPlan,
    _tally,
    apply_fallback_priced_events,
    fallback_price_row,
    plan_fallback_priced_events,
)
from tests.unit.data_health.conftest import make_event, make_price

_CHECK = FallbackPricedEventsCheck()
_TS = datetime(2026, 9, 1, tzinfo=UTC)
_KEY = "anthropic::sonnet-5"


def _prices(session, *, own=True):
    make_price(
        session,
        provider_id="anthropic",
        model_id="sonnet",
        input_per_mtok=3.0,
        output_per_mtok=15.0,
    )
    if own:
        make_price(
            session,
            provider_id="anthropic",
            model_id="sonnet-5",
            input_per_mtok=2.0,
            output_per_mtok=10.0,
        )


def _ev(session, event_id, *, cost, model="sonnet-5", **kw) -> int:
    base = {
        "provider_id": "anthropic",
        "account_id": "acct",
        "model_id": model,
        "tokens_input": 1_000_000,
        "tokens_output": 0,
        "cost_usd": cost,
        "cost_estimated_usd": 0.0,
        "ts": _TS,
    }
    base.update(kw)
    return make_event(session, event_id=event_id, **base).id  # type: ignore[return-value]


def _get(session, event_id) -> UsageEvent:
    session.expire_all()
    return session.get(UsageEvent, event_id)  # type: ignore[return-value]


def _groups(session):
    return {g.key: g for g in _CHECK.detect(session).groups}


def test_fallback_billed_events_are_listed_repriced_and_rollups_follow(session):
    _prices(session)
    a = _ev(session, "a", cost=3.0)
    b = _ev(session, "b", cost=3.0, ts=datetime(2026, 9, 2, tzinfo=UTC))
    session.add(
        UsagePeriodRollup(
            provider_id="anthropic",
            account_id="acct",
            period_type="all",
            period_key="all",
            model_id="sonnet-5",
            msgs=2,
            cost_usd=6.0,
        )
    )
    session.commit()

    group = _groups(session)[_KEY]
    assert group.count == 2 and group.fixable
    assert group.samples[0].detail["delta_usd"] == pytest.approx(-2.0)

    plan = _CHECK.plan(session, _KEY, {})
    assert plan.counts["events"] == 2
    assert plan.counts["stored_usd"] == pytest.approx(6.0)
    assert plan.counts["new_usd"] == pytest.approx(4.0)

    result = _CHECK.apply(session, _KEY, {})[0]
    assert result.counts["events"] == 2 and result.counts["rollups_rebuilt_pairs"] == 1
    for eid in (a, b):
        ev = _get(session, eid)
        assert ev.cost_usd == pytest.approx(2.0)
        assert ev.cost_estimated_usd == pytest.approx(2.0)
        assert ev.cost_input == pytest.approx(2.0)
    rollup_total = sum(r.cost_usd for r in session.exec(select(UsagePeriodRollup)).all())
    assert rollup_total > 0
    assert max(r.cost_usd for r in session.exec(select(UsagePeriodRollup)).all()) == pytest.approx(
        4.0
    )


def test_untouched_events(session):
    _prices(session)
    other = _ev(session, "other", cost=2.5)  # matches neither 3.0 nor 2.0
    reported = _ev(session, "rep", cost=3.0, cost_reported_usd=3.0)
    estimated = _ev(session, "est", cost=3.0, cost_estimated_usd=3.0)  # stale_priced_models' job
    at_today = _ev(session, "today", cost=2.0)  # already at today's rate
    zero = _ev(session, "zero", cost=0.0)

    assert _groups(session) == {}
    plan = plan_fallback_priced_events(session, "anthropic", "sonnet-5")
    assert plan.events == 0
    assert apply_fallback_priced_events(session, "anthropic", "sonnet-5").events == 0
    for eid, cost in (
        (other, 2.5),
        (reported, 3.0),
        (estimated, 3.0),
        (at_today, 2.0),
        (zero, 0.0),
    ):
        assert _get(session, eid).cost_usd == cost
    assert _get(session, reported).cost_reported_usd == 3.0
    assert _get(session, other).cost_estimated_usd == 0.0


def test_a_model_still_on_a_fallback_today_is_untouched(session):
    _prices(session, own=False)
    ev = _ev(session, "a", cost=3.0)
    assert _groups(session) == {}
    plan = plan_fallback_priced_events(session, "anthropic", "sonnet-5")
    assert plan.events == 0 and plan.no_exact_row == 1
    apply_fallback_priced_events(session, "anthropic", "sonnet-5")
    assert _get(session, ev).cost_estimated_usd == 0.0


def test_a_model_without_a_fallback_row_is_untouched(session):
    make_price(
        session,
        provider_id="anthropic",
        model_id="sonnet-5",
        input_per_mtok=2.0,
        output_per_mtok=10.0,
    )
    ev = _ev(session, "a", cost=3.0)
    assert _groups(session) == {}
    assert plan_fallback_priced_events(session, "anthropic", "sonnet-5").no_fallback_row == 1
    apply_fallback_priced_events(session, "anthropic", "sonnet-5")
    assert _get(session, ev).cost_usd == 3.0


def test_opencode_style_reported_events_are_untouched(session):
    make_price(
        session, provider_id="opencode", model_id="kimi-k3", input_per_mtok=1.0, output_per_mtok=1.0
    )
    legacy = _ev(session, "o1", provider_id="opencode", model="kimi-k3", cost=0.37)  # no family row
    reported = _ev(
        session, "o2", provider_id="opencode", model="kimi-k3", cost=0.37, cost_reported_usd=0.37
    )
    assert _groups(session) == {}
    apply_fallback_priced_events(session, "opencode", "kimi-k3")
    assert _get(session, legacy).cost_usd == 0.37
    assert _get(session, reported).cost_reported_usd == 0.37


def test_idempotent_and_per_group(session):
    _prices(session)
    make_price(
        session, provider_id="anthropic", model_id="opus", input_per_mtok=15.0, output_per_mtok=75.0
    )
    make_price(
        session,
        provider_id="anthropic",
        model_id="opus-5.5",
        input_per_mtok=5.0,
        output_per_mtok=25.0,
    )
    s = _ev(session, "s", cost=3.0)
    o = _ev(session, "o", cost=15.0, model="opus-5.5")
    assert set(_groups(session)) == {_KEY, "anthropic::opus-5.5"}

    _CHECK.apply(session, _KEY, {})
    assert _get(session, o).cost_usd == 15.0 and _get(session, o).cost_estimated_usd == 0.0
    again = _CHECK.apply(session, _KEY, {})[0]
    assert again.counts["events"] == 0
    assert set(_groups(session)) == {"anthropic::opus-5.5"}
    assert _get(session, s).cost_usd == pytest.approx(2.0)


def test_detect_hint_agrees_with_the_exact_plan_on_a_mixed_fixture(session):
    _prices(session)
    for i in range(5):
        _ev(session, f"m{i}", cost=3.0, ts=datetime(2026, 9, 1 + i, tzinfo=UTC))
    _ev(session, "x", cost=9.99, ts=datetime(2026, 9, 10, tzinfo=UTC))  # matches neither
    _ev(session, "r", cost=3.0, cost_reported_usd=3.0, ts=datetime(2026, 9, 11, tzinfo=UTC))
    _ev(session, "c", cost=3.0, cost_estimated_usd=3.0, ts=datetime(2026, 9, 12, tzinfo=UTC))

    detail = _groups(session)[_KEY].samples[0].detail
    plan = plan_fallback_priced_events(session, "anthropic", "sonnet-5")
    assert detail["events"] == plan.events == 5
    assert detail["stored_usd"] == pytest.approx(plan.stored_usd)
    assert detail["new_usd"] == pytest.approx(plan.new_usd)


def test_aggregate_cloud_events_use_the_standard_tier(session):
    make_price(
        session,
        provider_id="anthropic",
        model_id="sonnet",
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        long_prompt_threshold_tokens=200_000,
        long_input_per_mtok=6.0,
        long_output_per_mtok=22.0,
    )
    make_price(
        session,
        provider_id="anthropic",
        model_id="sonnet-5",
        input_per_mtok=2.0,
        output_per_mtok=10.0,
        long_prompt_threshold_tokens=200_000,
        long_input_per_mtok=4.0,
        long_output_per_mtok=15.0,
    )
    agg = _ev(session, "x", cost=3.0)
    row = _get(session, agg)
    row.event_id = "cloud:abc"
    session.add(row)
    session.commit()
    assert _groups(session)[_KEY].count == 1
    apply_fallback_priced_events(session, "anthropic", "sonnet-5")
    assert _get(session, agg).cost_usd == pytest.approx(2.0)  # standard tier, not 4.0


def test_events_at_both_rates_on_one_day_are_found_per_event(session):
    _prices(session)
    for i in range(4):
        _ev(session, f"f{i}", cost=3.0)
        _ev(session, f"t{i}", cost=2.0)

    assert _groups(session)[_KEY].count == 4
    assert plan_fallback_priced_events(session, "anthropic", "sonnet-5").events == 4


def test_long_prompt_tier_is_priced_per_event_in_detect(session):
    for model, lo, long_in in (("sonnet", 3.0, 6.0), ("sonnet-5", 2.0, 4.0)):
        make_price(
            session,
            provider_id="anthropic",
            model_id=model,
            input_per_mtok=lo,
            output_per_mtok=lo,
            long_prompt_threshold_tokens=200_000,
            long_input_per_mtok=long_in,
            long_output_per_mtok=long_in,
        )
    ev = _ev(session, "long", cost=6.0)  # 1M input: long tier at the family row

    detail = _groups(session)[_KEY].samples[0].detail
    assert detail["events"] == 1 and detail["delta_usd"] == pytest.approx(-2.0)
    apply_fallback_priced_events(session, "anthropic", "sonnet-5")
    assert _get(session, ev).cost_usd == pytest.approx(4.0)


def test_batches_commit_mid_scan_and_rebuild_every_pair(session, monkeypatch):
    monkeypatch.setattr("app.services.maintenance.fallback_priced_events._EVENT_BATCH", 3)
    _prices(session)
    ids = [
        _ev(session, f"b{i}", cost=3.0, account_id="a" if i % 2 else "b", ts=_TS.replace(day=1 + i))
        for i in range(10)
    ]
    result = apply_fallback_priced_events(session, "anthropic", "sonnet-5")
    assert result.events == 10 and result.rollups_rebuilt_pairs == 2
    assert all(_get(session, i).cost_usd == pytest.approx(2.0) for i in ids)
    assert apply_fallback_priced_events(session, "anthropic", "sonnet-5").events == 0


def test_unknown_skip_reason_is_rejected():
    with pytest.raises(ValueError):
        _tally(FallbackPlan(), "typo")


def test_malformed_group_key_is_rejected(session):
    with pytest.raises(ValueError):
        _CHECK.plan(session, "nonsense", {})


def _fresh(with_own: bool) -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    s = Session(engine)
    make_price(s, provider_id="anthropic", model_id="sonnet", input_per_mtok=3.0)
    make_price(s, provider_id="anthropic", model_id="gpt-5.7", input_per_mtok=7.0)
    if with_own:
        for m in ("sonnet-5", "gpt-5.7-nova"):
            make_price(s, provider_id="anthropic", model_id=m, input_per_mtok=1.0)
    return s


@pytest.mark.parametrize(
    "model", ["sonnet-5", "sonnet-5.5", "gpt-5.7-nova", "gpt-5.7-mini", "nothing-9"]
)
def test_fallback_helper_matches_resolve_price_row(model):
    on = date(2026, 9, 1)
    ts = datetime(2026, 9, 1, tzinfo=UTC)
    without = _fresh(False)
    with_own = _fresh(True)
    expected = resolve_price_row(without, "anthropic", model, ts)
    # No own row: the helper is exactly resolve_price_row's answer.
    got = fallback_price_row(PricingIndex.load(without), "anthropic", model, on)
    assert (got.model_id if got else None) == (expected.model_id if expected else None)
    # Own row present: the helper still returns what resolve_price_row used without it.
    got = fallback_price_row(PricingIndex.load(with_own), "anthropic", model, on)
    assert (got.model_id if got else None) == (expected.model_id if expected else None)
    own = resolve_price_row(with_own, "anthropic", model, ts)
    if model in ("sonnet-5", "gpt-5.7-nova"):
        assert own is not None and own.model_id == model


def test_fallback_helper_skips_free_tier_ids():
    s = _fresh(False)
    assert (
        fallback_price_row(PricingIndex.load(s), "anthropic", "sonnet-5:free", date(2026, 9, 1))
        is None
    )


def test_script_wrapper_dry_run_and_apply(monkeypatch, capsys):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as seed:
        _prices(seed)
        ev = _ev(seed, "a", cost=3.0)
    monkeypatch.setattr(script, "engine", engine)

    script.run("anthropic", "sonnet-5", True)
    assert "[DRY-RUN] 1 event(s)" in capsys.readouterr().out
    script.run("anthropic", "sonnet-5", False)
    assert "Re-priced 1 event(s)" in capsys.readouterr().out
    with Session(engine) as check:
        assert _get(check, ev).cost_usd == pytest.approx(2.0)
