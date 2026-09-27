"""Tests for app/services/maintenance/recost.py."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import ProviderConfig, ProviderPricing, UsageEvent, UsagePeriodRollup
from app.services.maintenance.recost import RecostChange, apply_recost, plan_recost


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _price(session: Session, provider_id: str, model_id: str, rate: float = 1.0) -> None:
    session.add(
        ProviderPricing(
            provider_id=provider_id,
            model_id=model_id,
            effective_from=date(2020, 1, 1),
            input_per_mtok=rate,
            output_per_mtok=rate,
            cache_read_per_mtok=0.0,
            cache_create_per_mtok=0.0,
        )
    )
    session.commit()


def _event(session: Session, **overrides) -> UsageEvent:
    base = {
        "provider_id": "chatgpt",
        "account_id": "alice@example.com",
        "sidecar_id": "dev-01",
        "event_id": f"msg_{overrides.get('event_id', 'x')}",
        "ts": datetime(2026, 9, 1, tzinfo=UTC),
        "kind": "message",
        "model_id": "gpt-6-sol",
        "tokens_input": 1_000_000,
        "tokens_output": 0,
        "cost_usd": 0.0,
    }
    base.update(overrides)
    ev = UsageEvent(**base)
    session.add(ev)
    session.commit()
    return ev


def test_plan_recost_finds_a_zero_cost_event_once_priced():
    session = _session()
    _event(session)
    _price(session, "chatgpt", "gpt-6-sol", rate=2.0)

    plan = plan_recost(session, ["chatgpt"])

    assert plan.updated == 1
    assert plan.zeroed == 0
    assert plan.affected_pairs == {("chatgpt", "alice@example.com")}
    assert len(plan.samples) == 1
    assert isinstance(plan.samples[0], RecostChange)
    assert plan.samples[0].resolved.cost_usd == 2.0  # 1M tokens @ $2/Mtok


def test_plan_recost_is_read_only():
    session = _session()
    _event(session)
    _price(session, "chatgpt", "gpt-6-sol", rate=2.0)

    plan_recost(session, ["chatgpt"])

    ev = session.exec(select(UsageEvent)).one()
    assert ev.cost_usd == 0.0  # untouched


def test_apply_recost_writes_the_new_cost_and_rebuilds_rollups():
    session = _session()
    _event(session)
    _price(session, "chatgpt", "gpt-6-sol", rate=2.0)

    result = apply_recost(session, ["chatgpt"])

    assert result.updated == 1
    ev = session.exec(select(UsageEvent)).one()
    assert ev.cost_usd == 2.0
    assert ev.cost_estimated_usd == 2.0
    lifetime = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).one()
    assert lifetime.cost_usd == 2.0
    assert result.rollups_rebuilt_pairs == 1


def test_only_zero_cost_never_lowers_an_existing_cost():
    """The Data Health unpriced_models fixer's core safety contract:
    re-running recost must never lower a cost that's already nonzero,
    even if a newer/cheaper price row now resolves."""
    session = _session()
    _event(session, cost_usd=50.0)  # already has a real cost (e.g. source-reported)
    _price(session, "chatgpt", "gpt-6-sol", rate=2.0)  # would compute to $2.0 — lower

    plan = plan_recost(session, ["chatgpt"], only_zero_cost=True)

    assert plan.updated == 0
    assert plan.zeroed == 0


def test_only_zero_cost_skips_rows_still_unpriced():
    session = _session()
    _event(session)  # no price row seeded at all — stays $0.0

    plan = plan_recost(session, ["chatgpt"], only_zero_cost=True)

    assert plan.updated == 0
    assert plan.skipped_still_unpriced == 1


def test_only_zero_cost_updates_a_newly_priced_zero_cost_row():
    session = _session()
    _event(session, cost_usd=0.0)
    _price(session, "chatgpt", "gpt-6-sol", rate=2.0)

    result = apply_recost(session, ["chatgpt"], only_zero_cost=True)

    assert result.updated == 1
    ev = session.exec(select(UsageEvent)).one()
    assert ev.cost_usd == 2.0


def test_since_filters_to_events_on_or_after_the_date():
    session = _session()
    _event(session, event_id="old", ts=datetime(2026, 8, 1, tzinfo=UTC))
    _event(session, event_id="new", ts=datetime(2026, 9, 5, tzinfo=UTC))
    _price(session, "chatgpt", "gpt-6-sol", rate=2.0)

    plan = plan_recost(session, ["chatgpt"], since=date(2026, 9, 1))

    assert plan.updated == 1
    assert plan.samples[0].event_ref == "new"


def test_pay_as_you_go_billing_type_is_looked_up_from_provider_config():
    session = _session()
    session.add(
        ProviderConfig(
            provider_id="chatgpt", account_id="alice@example.com", billing_type="pay_as_you_go"
        )
    )
    session.commit()
    _event(session, cost_usd=0.0, cost_reported_usd=0.0321)
    _price(session, "chatgpt", "gpt-6-sol", rate=2.0)  # would otherwise compute $2.0

    apply_recost(session, ["chatgpt"])

    ev = session.exec(select(UsageEvent)).one()
    assert ev.cost_usd == 0.0321  # trusts the report over the estimate


def test_legacy_opencode_events_derive_reported_cost_from_cost_usd():
    """OpenCode's legacy backends logged their subscription amount straight
    into cost_usd with no cost_reported_usd populated. Recost reads that
    back as the report (cost_usd itself resolves to the same total, since
    no price row exists) and, as a one-time migration side effect, backfills
    the previously-missing cost_reported_usd column."""
    session = _session()
    _event(
        session,
        provider_id="opencode",
        model_id="some-legacy-model",
        cost_usd=0.42,
        cost_reported_usd=None,
    )
    # No price row for "some-legacy-model" at all.
    result = apply_recost(session, ["opencode"])

    assert result.updated == 1
    ev = session.exec(select(UsageEvent)).one()
    assert ev.cost_usd == 0.42  # unchanged in value...
    assert ev.cost_reported_usd == 0.42  # ...but cost_reported_usd is now backfilled

    # Idempotent: a second pass finds nothing left to change.
    second = apply_recost(session, ["opencode"])
    assert second.updated == 0
    assert second.unchanged == 1


def test_apply_recost_skips_error_events():
    session = _session()
    _event(session, kind="error", cost_usd=0.0)
    _price(session, "chatgpt", "gpt-6-sol", rate=2.0)

    result = apply_recost(session, ["chatgpt"])

    assert result.updated == 0
    assert result.unchanged == 0
    assert result.skipped_still_unpriced == 0


def test_apply_recost_scopes_rollup_rebuild_to_affected_pairs_only():
    session = _session()
    _event(session, provider_id="chatgpt", account_id="alice@example.com", cost_usd=0.0)
    # bob's cost already matches what the seeded rate would resolve to
    # (1M tokens @ $2/Mtok = $2.0) — genuinely unchanged, not just close.
    _event(
        session,
        event_id="2",
        provider_id="chatgpt",
        account_id="bob@example.com",
        cost_usd=2.0,
        cost_estimated_usd=2.0,
    )
    _price(session, "chatgpt", "gpt-6-sol", rate=2.0)

    result = apply_recost(session, ["chatgpt"])

    assert ("chatgpt", "bob@example.com") not in result.affected_pairs
    assert ("chatgpt", "alice@example.com") in result.affected_pairs


def test_apply_recost_skip_rollups_and_windows_flags():
    session = _session()
    _event(session)
    _price(session, "chatgpt", "gpt-6-sol", rate=2.0)

    result = apply_recost(session, ["chatgpt"], skip_rollups=True, skip_windows=True)

    assert result.rollups_rebuilt_pairs == 0
    assert result.windows_rebuilt == 0
    assert list(session.exec(select(UsagePeriodRollup))) == []
