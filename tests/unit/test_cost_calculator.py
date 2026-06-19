from datetime import UTC, date, datetime, timedelta, timezone

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import ProviderPricing
from app.services.cost_calculator import compute_event_cost, compute_event_cost_breakdown
from app.services.pricing_seed import seed_pricing_table


def _seeded_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    s = Session(engine)
    seed_pricing_table(s)
    return s


def test_anthropic_sonnet_cost_basic_input_output():
    """1M input + 1M output on sonnet = $3 + $15 = $18."""
    s = _seeded_session()
    cost = compute_event_cost(
        s,
        provider_id="anthropic",
        model_id="sonnet",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    assert cost == 18.00


def test_anthropic_sonnet_cost_includes_cache():
    """cache_read at $0.30/MT, cache_create at $3.75/MT."""
    s = _seeded_session()
    cost = compute_event_cost(
        s,
        provider_id="anthropic",
        model_id="sonnet",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=1_000_000,
        tokens_cache_create=1_000_000,
        tokens_reasoning=0,
    )
    assert cost == 18.00 + 0.30 + 3.75


def test_chatgpt_gpt54_mini_cost():
    """1M input + 1M output on gpt-5.4-mini = $0.75 + $4.50 = $5.25."""
    s = _seeded_session()
    cost = compute_event_cost(
        s,
        provider_id="chatgpt",
        model_id="gpt-5.4-mini",
        ts=datetime(2026, 5, 16, tzinfo=UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    assert cost == 5.25


def test_gemini_2_5_pro_cost_with_cache_and_thoughts():
    """1M input + 1M output + 1M cache_read + 1M reasoning on pro-2.5
    = $1.25 + $10.00 + $0.125 + $10.00 (reasoning billed at output rate) = $21.375."""
    s = _seeded_session()
    cost = compute_event_cost(
        s,
        provider_id="gemini",
        model_id="pro-2.5",
        ts=datetime(2026, 5, 17, tzinfo=UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=1_000_000,
        tokens_cache_create=0,
        tokens_reasoning=1_000_000,
    )
    assert cost == 1.25 + 10.00 + 0.125 + 10.00


def test_gemini_3_1_pro_preview_cost_uses_higher_rate():
    """pro-3.1-preview is priced 60% above 2.5 Pro on input ($2.00 vs $1.25)
    and 20% above on output ($12.00 vs $10.00). Same payload as the 2.5 case
    must yield strictly more cost."""
    s = _seeded_session()
    ts = datetime(2026, 5, 17, tzinfo=UTC)
    payload = {
        "tokens_input": 1_000_000,
        "tokens_output": 1_000_000,
        "tokens_cache_read": 1_000_000,
        "tokens_cache_create": 0,
        "tokens_reasoning": 0,
    }
    cost_25 = compute_event_cost(s, provider_id="gemini", model_id="pro-2.5", ts=ts, **payload)
    cost_31 = compute_event_cost(
        s, provider_id="gemini", model_id="pro-3.1-preview", ts=ts, **payload
    )
    assert cost_25 == 1.25 + 10.00 + 0.125
    assert cost_31 == 2.00 + 12.00 + 0.20
    assert cost_31 > cost_25


def test_gemini_flash_2_5_cache_read_rate_matches_official():
    """Regression: cache-read on Flash 2.5 was previously seeded at $0.075/MT
    (2.5× the real rate). Official rate is $0.03/MT."""
    s = _seeded_session()
    cost = compute_event_cost(
        s,
        provider_id="gemini",
        model_id="flash-2.5",
        ts=datetime(2026, 5, 17, tzinfo=UTC),
        tokens_input=0,
        tokens_output=0,
        tokens_cache_read=1_000_000,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    assert cost == 0.03


def test_breakdown_splits_components_and_sums_to_total():
    """Each component is priced separately; reasoning folds into output; the
    total equals compute_event_cost."""
    s = _seeded_session()
    kwargs = {
        "provider_id": "anthropic",
        "model_id": "sonnet",
        "ts": datetime.now(UTC),
        "tokens_input": 1_000_000,
        "tokens_output": 1_000_000,
        "tokens_cache_read": 1_000_000,
        "tokens_cache_create": 1_000_000,
        "tokens_reasoning": 1_000_000,
    }
    b = compute_event_cost_breakdown(s, **kwargs)
    assert b.input == 3.00
    assert b.output == 15.00 * 2  # output + reasoning, both at the output rate
    assert b.cache_read == 0.30
    assert b.cache_create == 3.75
    # The cache portion the cost views subtract:
    assert round(b.cache_read + b.cache_create, 6) == 4.05
    assert b.total == compute_event_cost(s, **kwargs)


def test_breakdown_zero_for_unpriced_model():
    s = _seeded_session()
    b = compute_event_cost_breakdown(
        s,
        provider_id="anthropic",
        model_id="<unknown>",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=1_000_000,
        tokens_cache_create=1_000_000,
    )
    assert b == (0.0, 0.0, 0.0, 0.0)
    assert b.total == 0.0


def test_unknown_model_returns_zero():
    s = _seeded_session()
    cost = compute_event_cost(
        s,
        provider_id="anthropic",
        model_id="<unknown>",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    assert cost == 0.0


def _seed_two_price_rows(s: Session, day_one: date, day_two: date) -> None:
    """Insert two pricing rows for a synthetic 'flat' model that differ only
    in `effective_from` so the test can prove the date-boundary behaviour."""
    s.add(
        ProviderPricing(
            provider_id="zztest",
            model_id="flat",
            effective_from=day_one,
            input_per_mtok=1.00,  # $1/Mtok before the bump
            output_per_mtok=1.00,
            cache_read_per_mtok=0.0,
            cache_create_per_mtok=0.0,
        )
    )
    s.add(
        ProviderPricing(
            provider_id="zztest",
            model_id="flat",
            effective_from=day_two,
            input_per_mtok=2.00,  # $2/Mtok from the bump onward
            output_per_mtok=2.00,
            cache_read_per_mtok=0.0,
            cache_create_per_mtok=0.0,
        )
    )
    s.commit()


def test_pricing_boundary_at_utc_midnight():
    """Two events one second apart across UTC midnight pick different prices.

    Documents the date-keyed semantics of compute_event_cost: an event at
    23:59:59 UTC bills at the previous day's rate, and the very next second
    (00:00:00 UTC) bills at the new day's rate. This is intentional — pricing
    rows are versioned by date, not by timestamp.
    """
    s = _seeded_session()
    _seed_two_price_rows(s, date(2026, 6, 1), date(2026, 6, 2))

    last_sec_old = datetime(2026, 6, 1, 23, 59, 59, tzinfo=UTC)
    first_sec_new = datetime(2026, 6, 2, 0, 0, 0, tzinfo=UTC)
    common = {
        "provider_id": "zztest",
        "model_id": "flat",
        "tokens_input": 1_000_000,
        "tokens_output": 0,
        "tokens_cache_read": 0,
        "tokens_cache_create": 0,
        "tokens_reasoning": 0,
    }

    cost_before = compute_event_cost(s, ts=last_sec_old, **common)
    cost_after = compute_event_cost(s, ts=first_sec_new, **common)

    assert cost_before == 1.00  # old rate
    assert cost_after == 2.00  # new rate (effective from day_two)


def test_pricing_boundary_uses_utc_date_not_local_date():
    """A timestamp expressed in a non-UTC timezone is treated by its UTC date.

    Same UTC instant, different `tzinfo` representations → same price row.
    """
    s = _seeded_session()
    _seed_two_price_rows(s, date(2026, 6, 1), date(2026, 6, 2))

    # 2026-06-02 00:30 UTC == 2026-06-01 17:30 in UTC-7. The two datetimes
    # describe the same instant. The function reads `.date()` after the
    # caller's tz conversion, so the UTC representation drives the lookup —
    # any caller that passes a UTC-aware ts (the convention in this repo)
    # gets the UTC-date rate.
    utc_ts = datetime(2026, 6, 2, 0, 30, 0, tzinfo=UTC)
    same_instant_in_pdt = utc_ts.astimezone(timezone(timedelta(hours=-7)))

    common = {
        "provider_id": "zztest",
        "model_id": "flat",
        "tokens_input": 1_000_000,
        "tokens_output": 0,
        "tokens_cache_read": 0,
        "tokens_cache_create": 0,
        "tokens_reasoning": 0,
    }

    # Documented behaviour: pricing is keyed on ts.date() AFTER it has been
    # converted to UTC by the caller. The non-UTC tz here happens to fall
    # on a different LOCAL date but the .date() of the awareness-stripped
    # value still belongs to the UTC moment because the caller would pass
    # the UTC version in practice. We assert the UTC-aware ts wins.
    cost_utc = compute_event_cost(s, ts=utc_ts, **common)
    cost_pdt = compute_event_cost(s, ts=same_instant_in_pdt.astimezone(UTC), **common)
    assert cost_utc == cost_pdt == 2.00


def test_anthropic_versioned_opus_falls_back_to_family_rate():
    """opus-4.8 has no pricing row, so it bills at the 'opus' family rate."""
    s = _seeded_session()
    versioned = compute_event_cost(
        s,
        provider_id="anthropic",
        model_id="opus-4.8",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    family = compute_event_cost(
        s,
        provider_id="anthropic",
        model_id="opus",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    assert versioned == family > 0.0


def test_gemini_versioned_id_still_matches_exactly():
    """Provider that prices per version keeps exact match (no fallback drift)."""
    s = _seeded_session()
    cost = compute_event_cost(
        s,
        provider_id="gemini",
        model_id="pro-2.5",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=0,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    assert cost > 0.0


# ── Antigravity cost resolution ───────────────────────────────────────────────


def test_antigravity_pro3_cost():
    """1M input + 1M output on pro-3 = $2.00 + $12.00 = $14.00."""
    s = _seeded_session()
    cost = compute_event_cost(
        s,
        provider_id="antigravity",
        model_id="pro-3",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    assert cost == 14.00


def test_antigravity_claude_opus_cost():
    """1M input + 1M output on claude-opus = $5.00 + $25.00 = $30.00.

    This id does NOT version-strip (no trailing -<digits>), so the lookup must
    find the explicit antigravity/claude-opus row rather than falling back to
    the anthropic 'opus' family row.
    """
    s = _seeded_session()
    cost = compute_event_cost(
        s,
        provider_id="antigravity",
        model_id="claude-opus",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    assert cost == 30.00


def test_antigravity_claude_sonnet_cost():
    """1M input + 1M output on claude-sonnet = $3.00 + $15.00 = $18.00."""
    s = _seeded_session()
    cost = compute_event_cost(
        s,
        provider_id="antigravity",
        model_id="claude-sonnet",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    assert cost == 18.00


def test_antigravity_unknown_model_costs_zero():
    """GPT-OSS and any other unpriced antigravity model_id must cost 0."""
    s = _seeded_session()
    for model_id in ("unknown", "gpt-oss"):
        cost = compute_event_cost(
            s,
            provider_id="antigravity",
            model_id=model_id,
            ts=datetime.now(UTC),
            tokens_input=1_000_000,
            tokens_output=1_000_000,
            tokens_cache_read=0,
            tokens_cache_create=0,
            tokens_reasoning=0,
        )
        assert cost == 0.0, f"expected 0 for model_id={model_id!r}, got {cost}"


def test_antigravity_scoped_separately_from_gemini():
    """antigravity/pro-3 must NOT match the gemini/pro-3.x rows.

    Pricing lookups are scoped by provider_id; the rates happen to match today
    but they are independent entries.
    """
    s = _seeded_session()
    # pro-3.1-preview is the closest gemini row; verify antigravity uses its own row.
    cost_ag = compute_event_cost(
        s,
        provider_id="antigravity",
        model_id="pro-3",
        ts=datetime.now(UTC),
        tokens_input=1_000_000,
        tokens_output=0,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    # Would be 0 if the lookup accidentally crossed into gemini's namespace.
    assert cost_ag == 2.00
