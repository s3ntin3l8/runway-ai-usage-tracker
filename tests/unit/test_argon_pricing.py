"""Tests for Gemini 4 Argon pricing and the stored-id repair (issue #686).

Google prices Gemini 4 Argon as one family under the id "gemini-4-argon"
(https://ai.google.dev/gemini-api/docs/pricing). The Antigravity extractor
reports the raw id as "gemini-4-argon-trial", which used to survive into
`usage_events.model_id` and leave those events at $0.00.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import ProviderConfig, ProviderPricing, UsageEvent, UsagePeriodRollup
from app.services.cost_calculator import resolve_price_row
from app.services.maintenance.argon_retag import apply_argon_retag, plan_argon_retag
from app.services.model_normalization import (
    ARGON_MODEL_ID,
    normalize_ag_model,
    normalize_gemini_model,
)
from app.services.period_rollups import rebuild_rollups_for_pairs
from app.services.pricing_seed import seed_pricing_table

TS = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _event(session: Session, **overrides) -> UsageEvent:
    base = {
        "provider_id": "antigravity",
        "account_id": "alice@example.com",
        "sidecar_id": "dev-01",
        "event_id": "conv-1|gen_0",
        "ts": TS,
        "kind": "message",
        "model_id": "gemini-4-argon-trial",
        "tokens_input": 1_000_000,
        "tokens_output": 1_000_000,
        "cost_usd": 0.0,
    }
    base.update(overrides)
    ev = UsageEvent(**base)
    session.add(ev)
    session.commit()
    return ev


# ---------------------------------------------------------------------------
# Seeded pricing rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["antigravity", "gemini"])
def test_seeded_argon_rows_match_the_published_rates(provider):
    """$2/$10/$0.10 through 2026-11-30, then $4/$20/$0.20 (checked 2026-10-20)."""
    session = _session()
    seed_pricing_table(session)

    promo = session.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == provider,
            ProviderPricing.model_id == ARGON_MODEL_ID,
            ProviderPricing.effective_from == date(2026, 10, 1),
        )
    ).first()
    assert promo is not None
    assert (promo.input_per_mtok, promo.output_per_mtok, promo.cache_read_per_mtok) == (
        2.00,
        10.00,
        0.10,
    )

    listed = session.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == provider,
            ProviderPricing.model_id == ARGON_MODEL_ID,
            ProviderPricing.effective_from == date(2026, 12, 1),
        )
    ).first()
    assert listed is not None
    assert (listed.input_per_mtok, listed.output_per_mtok, listed.cache_read_per_mtok) == (
        4.00,
        20.00,
        0.20,
    )


@pytest.mark.parametrize("provider", ["antigravity", "gemini"])
def test_normalized_id_is_the_seeded_pricing_key(provider):
    """The id the normalizers produce is the row key — no fallback needed."""
    session = _session()
    seed_pricing_table(session)

    row = resolve_price_row(session, provider, ARGON_MODEL_ID, TS)
    assert row is not None
    assert row.model_id == ARGON_MODEL_ID
    assert row.input_per_mtok == 2.00
    assert row.output_per_mtok == 10.00


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "display"),
    [
        ("gemini-4-argon-trial", "Gemini 4 Argon (High)"),
        ("gemini-4-argon-trial", ""),
        ("gemini-4-argon", "Gemini 4 Argon"),
        ("", "Gemini 4 Argon (High)"),
    ],
)
def test_antigravity_normalizer_agrees_on_the_canonical_id(raw, display):
    assert normalize_ag_model(raw, display, {}) == ARGON_MODEL_ID


@pytest.mark.parametrize(
    "raw",
    ["gemini-4-argon-trial", "gemini-4-argon", "gemini-4-argon-preview"],
)
def test_gemini_normalizer_agrees_on_the_canonical_id(raw):
    """The Gemini normalizer only sees a model id, never a display name."""
    assert normalize_gemini_model(raw) == ARGON_MODEL_ID


def test_stored_ids_never_keep_the_trial_suffix():
    """Every ingest path lands on the seeded key, not the raw slug."""
    assert normalize_ag_model("gemini-4-argon-trial", "", {}) == ARGON_MODEL_ID
    assert normalize_gemini_model("gemini-4-argon-trial") == ARGON_MODEL_ID


# ---------------------------------------------------------------------------
# The stored-id repair
# ---------------------------------------------------------------------------


def test_plan_reports_the_unpriced_argon_event():
    session = _session()
    seed_pricing_table(session)
    _event(session)

    plan = plan_argon_retag(session)

    assert plan.total == 1
    assert plan.by_model == {"gemini-4-argon-trial": 1}
    assert plan.repriced == 1
    assert plan.unpriced == 0
    assert plan.cost_delta_usd == pytest.approx(12.0)


def test_apply_rewrites_the_id_and_prices_the_event():
    session = _session()
    seed_pricing_table(session)
    _event(session)

    result = apply_argon_retag(session)

    assert result.retagged == 1
    assert result.repriced == 1
    ev = session.exec(select(UsageEvent)).first()
    assert ev is not None
    assert ev.model_id == ARGON_MODEL_ID
    # 1M input at $2/MTok + 1M output at $10/MTok.
    assert ev.cost_usd == pytest.approx(12.0)
    assert ev.cost_estimated_usd == pytest.approx(12.0)
    assert ev.cost_input == pytest.approx(2.0)
    assert ev.cost_output == pytest.approx(10.0)


def test_apply_rebuilds_the_rollups_under_the_canonical_grain():
    session = _session()
    seed_pricing_table(session)
    _event(session)

    apply_argon_retag(session)

    rows = session.exec(select(UsagePeriodRollup)).all()
    model_ids = {row.model_id for row in rows}
    assert "gemini-4-argon-trial" not in model_ids
    canonical = [row for row in rows if row.model_id == ARGON_MODEL_ID]
    assert canonical
    assert all(row.cost_usd == pytest.approx(12.0) for row in canonical)


def test_repair_is_idempotent():
    """A repeat run on a repaired database changes nothing and replays nothing."""
    session = _session()
    seed_pricing_table(session)
    _event(session)

    apply_argon_retag(session)
    second = apply_argon_retag(session)

    assert second.retagged == 0
    assert second.cost_delta_usd == 0.0
    # The rebuild is gated on "something changed or a stale grain is left",
    # so a repeat does not replay the rollups and windows.
    assert second.rollups_rebuilt_pairs == 0
    assert second.windows_rebuilt == 0


def test_an_interrupted_repair_is_completed_by_a_second_run():
    """A crash after the event writes but before the rebuild leaves the stale
    rollup grain behind — the next run has to find it from the drift."""
    session = _session()
    seed_pricing_table(session)
    _event(session)
    # Roll the raw id up first, the way ingest would have.
    rebuild_rollups_for_pairs(session, {("antigravity", "alice@example.com")})
    assert "gemini-4-argon-trial" in {
        row.model_id for row in session.exec(select(UsagePeriodRollup))
    }

    # First run: events rewritten, rebuild skipped (the crash point).
    first = apply_argon_retag(session, skip_rollups=True)
    assert first.retagged == 1
    assert "gemini-4-argon-trial" in {
        row.model_id for row in session.exec(select(UsagePeriodRollup))
    }

    # Second run: nothing left to rewrite, but the stale grain is still there.
    second = apply_argon_retag(session)
    assert second.retagged == 0
    assert second.rollups_rebuilt_pairs == 1
    model_ids = {row.model_id for row in session.exec(select(UsagePeriodRollup))}
    assert "gemini-4-argon-trial" not in model_ids
    assert ARGON_MODEL_ID in model_ids


def test_the_plan_reports_the_drift_on_a_partially_repaired_database():
    session = _session()
    seed_pricing_table(session)
    _event(session)
    rebuild_rollups_for_pairs(session, {("antigravity", "alice@example.com")})
    apply_argon_retag(session, skip_rollups=True)

    plan = plan_argon_retag(session)

    assert plan.total == 0
    assert plan.stale_rollup_pairs == 1
    assert plan.rebuild_pairs == 1


def test_an_event_before_the_promo_row_stays_unpriced():
    """The promo row starts 2026-10-01, so an earlier event has no applicable row.

    The model id is still canonicalised — only the price is missing, which is
    what the operators have to watch for when they backdate the seed row.
    """
    session = _session()
    seed_pricing_table(session)
    _event(session, ts=datetime(2026, 9, 15, 12, 0, tzinfo=UTC))

    plan = plan_argon_retag(session)
    assert plan.total == 1
    assert plan.unpriced == 1
    assert plan.repriced == 0

    result = apply_argon_retag(session)
    assert result.unpriced == 1
    assert result.repriced == 0
    ev = session.exec(select(UsageEvent)).first()
    assert ev is not None
    assert ev.model_id == ARGON_MODEL_ID
    assert ev.cost_usd == 0.0


def test_the_plan_promises_no_rebuild_on_a_repaired_database():
    """The preview must agree with what an apply would actually do."""
    session = _session()
    seed_pricing_table(session)
    _event(session)
    apply_argon_retag(session)

    plan = plan_argon_retag(session)

    assert plan.total == 0
    assert plan.stale_rollup_pairs == 0
    assert plan.rebuild_pairs == 0


def test_a_raw_id_with_its_own_family_beats_an_argon_display():
    """The display only decides when the raw id names no family of its own."""
    assert normalize_ag_model("gemini-3.8-flash", "Gemini 4 Argon (High)", {}) == "flash-3.8"


def test_lookalike_model_id_is_left_alone():
    """A slug that merely starts like Argon is not part of the family."""
    session = _session()
    seed_pricing_table(session)
    _event(session, model_id="gemini-4-argonoid", event_id="conv-1|gen_1")

    plan = plan_argon_retag(session)

    assert plan.total == 0
    apply_argon_retag(session)
    ev = session.exec(select(UsageEvent)).first()
    assert ev is not None
    assert ev.model_id == "gemini-4-argonoid"


def test_provider_config_billing_type_is_respected():
    """A pay-as-you-go account keeps a source-reported cost."""
    session = _session()
    seed_pricing_table(session)
    session.add(
        ProviderConfig(
            provider_id="antigravity",
            account_id="alice@example.com",
            billing_type="pay_as_you_go",
        )
    )
    session.commit()
    _event(session, cost_reported_usd=1.5)

    apply_argon_retag(session)

    ev = session.exec(select(UsageEvent)).first()
    assert ev is not None
    assert ev.cost_usd == pytest.approx(1.5)
    assert ev.cost_estimated_usd == pytest.approx(12.0)
