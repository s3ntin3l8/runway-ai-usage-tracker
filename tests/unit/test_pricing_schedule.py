"""Time-of-day rate schedules: Python calculator, SQL twin, seeds, migration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select as sa_select
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import _add_columns_if_missing
from app.models.db import ProviderPricing, UsageEvent
from app.services.cost_calculator import compute_event_cost_breakdown
from app.services.maintenance.recost import apply_recost, plan_recost
from app.services.maintenance.sql_cost import sql_cost
from app.services.pricing_schedule import SCHEDULES, in_window
from app.services.pricing_seed import seed_pricing_table

# 2026-11-03 is a Tuesday; 11-04 Wednesday; 11-07 Saturday; 11-08 Sunday.
TUE, WED, SAT, SUN = (datetime(2026, 11, d, tzinfo=UTC) for d in (3, 4, 7, 8))


def at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        seed_pricing_table(s)
        yield s


def _cost(session: Session, provider: str, model: str, ts: datetime) -> float:
    return compute_event_cost_breakdown(
        session,
        provider_id=provider,
        model_id=model,
        ts=ts,
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=1_000_000,
        tokens_cache_create=0,
    ).total


def test_deepseek_peak_is_double_on_weekdays_only(session):
    base = _cost(session, "deepseek", "deepseek-v4-pro", at(TUE, 12))
    assert base == pytest.approx(0.66 + 1.98 + 0.022)
    assert _cost(session, "deepseek", "deepseek-v4-pro", at(TUE, 2)) == pytest.approx(2 * base)
    assert _cost(session, "deepseek", "deepseek-v4-pro", at(SAT, 2)) == pytest.approx(base)


@pytest.mark.parametrize(
    ("hour", "minute", "peak"),
    [
        (0, 59, False),
        (1, 0, True),
        (3, 59, True),
        (4, 0, False),
        (5, 59, False),
        (6, 0, True),
        (9, 59, True),
        (10, 0, False),
    ],
)
def test_deepseek_peak_boundaries(session, hour, minute, peak):
    base = _cost(session, "deepseek", "deepseek-flash", at(TUE, 12))
    got = _cost(session, "deepseek", "deepseek-flash", at(TUE, hour, minute))
    assert got == pytest.approx(base * (2 if peak else 1))


def test_ollama_offpeak_is_half_price(session):
    std = _cost(session, "ollama", "deepseek-v4-pro", at(WED, 13))
    assert std == pytest.approx(1.32 + 3.96 + 0.044)
    assert _cost(session, "ollama", "deepseek-v4-pro", at(WED, 20)) == pytest.approx(std / 2)
    assert _cost(session, "ollama", "deepseek-v4-pro", at(SUN, 13)) == pytest.approx(std / 2)


@pytest.mark.parametrize(
    ("hour", "minute", "standard"),
    [(11, 59, False), (12, 0, True), (17, 59, True), (18, 0, False), (0, 0, False)],
)
def test_ollama_boundaries(session, hour, minute, standard):
    std = _cost(session, "ollama", "deepseek-v4.1-flash", at(WED, 12))
    got = _cost(session, "ollama", "deepseek-v4.1-flash", at(WED, hour, minute))
    assert got == pytest.approx(std if standard else std / 2)


def test_non_utc_timestamp_is_converted_before_windowing():
    from datetime import timezone

    local = datetime(2026, 11, 3, 10, 0, tzinfo=timezone(timedelta(hours=8)))  # 02:00 UTC Tue
    assert in_window("deepseek_peak", local) is True
    assert in_window("deepseek_peak", local + timedelta(hours=2)) is False  # 04:00 UTC


def test_naive_timestamp_is_treated_as_utc():
    assert in_window("deepseek_peak", datetime(2026, 11, 3, 2, 0)) is True
    assert in_window("nope", datetime(2026, 11, 3, 2, 0)) is False


def test_rows_without_a_schedule_are_flat(session):
    a = _cost(session, "ollama", "gemma4", at(WED, 20))
    b = _cost(session, "ollama", "gemma4", at(WED, 13))
    assert a == b > 0


def test_unknown_schedule_name_is_ignored(session):
    row = session.exec(
        select(ProviderPricing).where(ProviderPricing.model_id == "deepseek-flash")
    ).one()
    row.rate_schedule = "retired_schedule"
    session.add(row)
    session.commit()
    assert _cost(session, "deepseek", "deepseek-flash", at(TUE, 2)) == pytest.approx(
        _cost(session, "deepseek", "deepseek-flash", at(TUE, 12))
    )


def test_sql_cost_agrees_with_python_across_every_hour_of_a_week(session):
    models = [("deepseek", "deepseek-v4-pro"), ("ollama", "deepseek-v4.1-flash")]
    n = 0
    for provider, model in models:
        for day in range(7):
            for hour in range(24):
                ts = datetime(2026, 11, 2 + day, hour, 30, tzinfo=UTC)  # Mon..Sun
                session.add(
                    UsageEvent(
                        provider_id=provider,
                        account_id="a",
                        event_id=f"{provider}-{day}-{hour}",
                        ts=ts,
                        kind="message",
                        model_id=model,
                        tokens_input=123_456,
                        tokens_output=7_890,
                        tokens_cache_read=1_000_000,
                        cost_usd=0.0,
                    )
                )
                n += 1
    session.commit()
    for provider, model in models:
        row = session.exec(
            select(ProviderPricing).where(
                ProviderPricing.provider_id == provider, ProviderPricing.model_id == model
            )
        ).one()
        rows = session.execute(
            sa_select(UsageEvent.ts, sql_cost(row)).where(
                UsageEvent.provider_id == provider, UsageEvent.model_id == model
            )
        ).all()
        assert len(rows) == 7 * 24
        for ts, sql_total in rows:
            expected = compute_event_cost_breakdown(
                session,
                provider_id=provider,
                model_id=model,
                ts=ts.replace(tzinfo=UTC),
                tokens_input=123_456,
                tokens_output=7_890,
                tokens_cache_read=1_000_000,
                tokens_cache_create=0,
            ).total
            assert sql_total == pytest.approx(expected, abs=2e-6), (provider, ts)


def test_known_schedules(session):
    assert set(SCHEDULES) == {"deepseek_peak", "ollama_offpeak"}


def test_seed_backfills_schedule_onto_existing_rows_and_keeps_hand_set_ones(session):
    flash, pro = (
        session.exec(
            select(ProviderPricing).where(
                ProviderPricing.provider_id == "deepseek", ProviderPricing.model_id == m
            )
        ).one()
        for m in ("deepseek-flash", "deepseek-v4-pro")
    )
    flash.rate_schedule = flash.rate_schedule_multiplier = None
    pro.rate_schedule, pro.rate_schedule_multiplier = "deepseek_peak", 3.0
    session.add(flash)
    session.add(pro)
    session.commit()

    assert seed_pricing_table(session) == []
    session.refresh(flash)
    session.refresh(pro)
    assert (flash.rate_schedule, flash.rate_schedule_multiplier) == ("deepseek_peak", 2.0)
    assert pro.rate_schedule_multiplier == 3.0


def test_init_db_migration_adds_columns_and_keeps_existing_rows():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        # Recreate a pre-change database: the table without the new columns.
        conn.execute(text("ALTER TABLE provider_pricing DROP COLUMN rate_schedule"))
        conn.execute(text("ALTER TABLE provider_pricing DROP COLUMN rate_schedule_multiplier"))
        conn.execute(
            text(
                "INSERT INTO provider_pricing (provider_id, model_id, effective_from, "
                "input_per_mtok, output_per_mtok, cache_read_per_mtok, cache_create_per_mtok, "
                "cache_create_1h_per_mtok) VALUES ('deepseek', 'm', '2026-01-01', 1, 2, 3, 4, 0)"
            )
        )
        conn.commit()
        _add_columns_if_missing(conn)
        _add_columns_if_missing(conn)  # idempotent
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(provider_pricing)"))}
        assert {"rate_schedule", "rate_schedule_multiplier"} <= cols
        row = conn.execute(
            text(
                "SELECT input_per_mtok, rate_schedule, rate_schedule_multiplier FROM provider_pricing"
            )
        ).one()
        assert tuple(row) == (1.0, None, None)


def _recost_event(session, event_id, provider, model, ts, cost):
    session.add(
        UsageEvent(
            provider_id=provider,
            account_id="a",
            event_id=event_id,
            ts=ts,
            kind="message",
            model_id=model,
            tokens_input=1_000_000,
            cost_usd=cost,
            cost_estimated_usd=cost,
        )
    )


def test_recost_reprices_only_scheduled_pairs_and_is_idempotent(session):
    # Stored at the flat base rate (pre-change behaviour).
    _recost_event(session, "peak", "deepseek", "deepseek-v4-pro", at(TUE, 2), 0.66)
    _recost_event(session, "off", "deepseek", "deepseek-v4-pro", at(TUE, 12), 0.66)
    _recost_event(session, "ol", "ollama", "deepseek-v4-pro", at(SUN, 13), 1.32)
    _recost_event(session, "flat", "ollama", "gemma4", at(SUN, 13), 0.14)
    session.commit()

    plan = plan_recost(session, ["deepseek", "ollama"], estimate_only=True)
    assert plan.updated == 2  # the deepseek peak event and the ollama weekend event
    result = apply_recost(session, ["deepseek", "ollama"], estimate_only=True)
    assert result.updated == 2
    got = {e.event_id: e.cost_usd for e in session.exec(select(UsageEvent)).all()}
    assert got["peak"] == pytest.approx(1.32)
    assert got["off"] == pytest.approx(0.66)
    assert got["ol"] == pytest.approx(0.66)
    assert got["flat"] == pytest.approx(0.14)

    again = apply_recost(session, ["deepseek", "ollama"], estimate_only=True)
    assert again.updated == 0 and again.zeroed == 0
