"""Tests for app/services/maintenance/event_cost.py::resolve_event_cost —
the single cost-decision rule shared by ingest and recost.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import ProviderPricing
from app.services.maintenance.event_cost import resolve_event_cost
from app.services.pricing_seed import seed_pricing_table


def _seeded_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    s = Session(engine)
    seed_pricing_table(s)
    return s


_COMMON = {
    "provider_id": "anthropic",
    "model_id": "sonnet",
    "ts": datetime.now(UTC),
    "tokens_input": 1_000_000,
    "tokens_output": 1_000_000,
    "tokens_cache_read": 0,
    "tokens_cache_create": 0,
}


def test_subscription_always_uses_the_computed_estimate():
    s = _seeded_session()
    result = resolve_event_cost(s, **_COMMON, billing_type="subscription", reported_cost=999.0)
    assert result.cost_usd == result.breakdown.total == 18.00
    assert result.cost_reported_usd == 999.0  # kept for the record, just not billed


def test_pay_as_you_go_trusts_the_reported_cost():
    s = _seeded_session()
    result = resolve_event_cost(s, **_COMMON, billing_type="pay_as_you_go", reported_cost=0.0123)
    assert result.cost_usd == 0.0123
    assert result.cost_estimated_usd == 18.00  # still recorded, just not billed


def test_pay_as_you_go_with_no_reported_cost_falls_back_to_estimate():
    s = _seeded_session()
    result = resolve_event_cost(s, **_COMMON, billing_type="pay_as_you_go", reported_cost=None)
    assert result.cost_usd == 18.00


def test_unknown_billing_with_no_price_row_trusts_the_reported_cost():
    """The generalized rule: any provider, not just OpenCode — no seeded
    price row and a reported cost available means trust the report rather
    than bill $0.00 for an unseeded model."""
    s = _seeded_session()
    common = {**_COMMON, "model_id": "some-model-nobody-seeded"}
    result = resolve_event_cost(s, **common, billing_type="unknown", reported_cost=4.2)
    assert result.cost_usd == 4.2
    assert result.cost_estimated_usd == 0.0


def test_unknown_billing_with_no_price_row_and_no_reported_cost_bills_zero():
    s = _seeded_session()
    common = {**_COMMON, "model_id": "some-model-nobody-seeded"}
    result = resolve_event_cost(s, **common, billing_type="unknown", reported_cost=None)
    assert result.cost_usd == 0.0


def test_unknown_billing_with_a_seeded_row_uses_the_estimate_not_the_report():
    """A resolvable price row wins even under unknown billing — this is
    what distinguishes "unseeded model" from "a model that legitimately
    isn't billed the reported way." Applies regardless of provider prefix,
    unlike the old opencode-only carve-out."""
    s = _seeded_session()
    result = resolve_event_cost(s, **_COMMON, billing_type="unknown", reported_cost=999.0)
    assert result.cost_usd == 18.00


def test_zero_rated_seeded_model_is_not_confused_with_unseeded():
    """A seeded row whose rates are all 0 (a genuinely free model) must
    still resolve to the computed $0.0, not fall back to a reported cost —
    resolve_price_row finds a row, so this isn't the "no row" branch."""
    s = _seeded_session()
    s.add(
        ProviderPricing(
            provider_id="anthropic",
            model_id="free-tier-model",
            effective_from=datetime(2020, 1, 1, tzinfo=UTC).date(),
            input_per_mtok=0.0,
            output_per_mtok=0.0,
            cache_read_per_mtok=0.0,
            cache_create_per_mtok=0.0,
        )
    )
    s.commit()
    common = {**_COMMON, "model_id": "free-tier-model"}
    result = resolve_event_cost(s, **common, billing_type="unknown", reported_cost=5.0)
    assert result.cost_usd == 0.0


def test_1h_5m_split_is_honored_not_dropped():
    """The bug this module fixes: recost_events.py's Phase B never passed
    the split through, so re-running it re-priced already-split Anthropic
    cache-create tokens entirely at the 5m rate. resolve_event_cost must
    honor it whenever a caller supplies it."""
    s = _seeded_session()
    common = {**_COMMON, "model_id": "fable", "tokens_cache_create": 2_000_000}
    with_split = resolve_event_cost(
        s,
        **common,
        tokens_cache_create_1h=1_000_000,
        tokens_cache_create_5m=1_000_000,
        billing_type="subscription",
        reported_cost=None,
    )
    without_split = resolve_event_cost(
        s,
        **common,
        billing_type="subscription",
        reported_cost=None,
    )
    # fable seeds a dedicated 1h rate (2x input) distinct from the 5m rate
    # (1.25x input) — a real split must cost more than treating it all as 5m.
    assert with_split.breakdown.cache_create > without_split.breakdown.cache_create
