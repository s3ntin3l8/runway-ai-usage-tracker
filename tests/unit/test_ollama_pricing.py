"""Ollama Cloud pricing rows (ollama.com/pricing) and their resolution."""

from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import ProviderPricing, UsageEvent
from app.services.cost_calculator import compute_event_cost_breakdown, resolve_price_row
from app.services.data_health.checks.unpriced_models import _classify_events
from app.services.maintenance.recost import apply_recost
from app.services.pricing_seed import PRICING_SEED, seed_pricing_table

TS = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        seed_pricing_table(s)
        yield s


def _event(session: Session, event_id: str, model_id: str, **tokens) -> UsageEvent:
    ev = UsageEvent(
        provider_id="ollama",
        account_id="user@test.com",
        event_id=event_id,
        ts=TS,
        kind="message",
        model_id=model_id,
        cost_usd=0.0,
        **tokens,
    )
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return ev


def test_seeded_ollama_model_ids_are_unique_per_effective_from():
    counts = Counter(
        (r["model_id"], r["effective_from"]) for r in PRICING_SEED if r["provider_id"] == "ollama"
    )
    assert len(counts) >= 22
    assert [k for k, n in counts.items() if n > 1] == []


def test_nemotron_ultra_cache_heavy_event_cost(session):
    # 170,000 prompt tokens, 96% cache read: 163,200 cached + 6,800 fresh.
    b = compute_event_cost_breakdown(
        session,
        provider_id="ollama",
        model_id="nemotron-3-ultra",
        ts=TS,
        tokens_input=6_800,
        tokens_output=88,
        tokens_cache_read=163_200,
        tokens_cache_create=0,
    )
    # 6800*0.10 + 163200*0.10 + 88*3.00, per 1M tokens
    assert b.total == pytest.approx(0.017264)


@pytest.mark.parametrize(
    ("model_id", "row_id"),
    [
        ("gemma4:31b", "gemma4"),
        ("gemma4", "gemma4"),
        ("gpt-oss:120b", "gpt-oss:120b"),
        ("gpt-oss:20b", "gpt-oss:20b"),
        ("kimi-k2.6", "kimi-k2.6"),
        ("deepseek-v4.1-flash", "deepseek-v4.1-flash"),
    ],
)
def test_ollama_model_resolution(session, model_id, row_id):
    row = resolve_price_row(session, "ollama", model_id, TS)
    assert row is not None
    assert row.model_id == row_id


def test_tag_fallback_is_scoped_to_ollama(session):
    # Same shape on another provider must not borrow the ollama row.
    assert resolve_price_row(session, "deepseek", "gemma4:31b", TS) is None


def test_free_tier_tag_does_not_reach_paid_ollama_row(session):
    assert resolve_price_row(session, "ollama", "gemma4:free", TS) is None


def test_backdated_rows_do_not_touch_other_providers(session):
    before = compute_event_cost_breakdown(
        session,
        provider_id="opencode",
        model_id="deepseek-v4-pro",
        ts=TS,
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=0,
        tokens_cache_create=0,
    )
    assert before.total == pytest.approx(1.74 + 3.48)
    row = resolve_price_row(session, "deepseek", "deepseek-v4-pro", TS)
    assert row is not None
    assert row.input_per_mtok == 0.66


def test_web_search_stays_unpriced_and_is_reported(session):
    ev = _event(session, "ws1", "web_search", tokens_input=5)
    assert resolve_price_row(session, "ollama", "web_search", TS) is None
    buckets = _classify_events(session)[("ollama", "web_search")]
    assert buckets["needs_seed_row"] == 1
    apply_recost(session, providers=["ollama"], only_zero_cost=True, skip_windows=True)
    session.refresh(ev)
    assert ev.cost_usd == 0.0


def test_recost_prices_zero_cost_ollama_events_and_is_idempotent(session):
    priced = _event(session, "e1", "gemma4:31b", tokens_input=1_000_000, tokens_output=1_000_000)
    other = UsageEvent(
        provider_id="deepseek",
        account_id="user@test.com",
        event_id="d1",
        ts=TS,
        kind="message",
        model_id="deepseek-v4-pro",
        cost_usd=0.0,
        tokens_input=1_000_000,
    )
    session.add(other)
    session.commit()

    first = apply_recost(session, providers=["ollama"], only_zero_cost=True, skip_windows=True)
    session.refresh(priced)
    session.refresh(other)
    assert first.updated == 1
    assert priced.cost_usd == pytest.approx(0.14 + 0.40)
    assert other.cost_usd == 0.0  # other providers untouched

    second = apply_recost(session, providers=["ollama"], only_zero_cost=True, skip_windows=True)
    session.refresh(priced)
    assert second.updated == 0
    assert priced.cost_usd == pytest.approx(0.14 + 0.40)
    assert (
        len(
            session.exec(
                select(ProviderPricing).where(ProviderPricing.provider_id == "ollama")
            ).all()
        )
        >= 22
    )
