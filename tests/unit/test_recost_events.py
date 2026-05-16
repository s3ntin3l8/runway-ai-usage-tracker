"""Unit tests for the recost_events backfill script."""

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup, UsageWindow
from app.services.pricing_seed import seed_pricing_table
from app.services.window_closer import close_window
from scripts.recost_events import phase_b_recost, phase_c_rollups, phase_d_windows


def _make_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    s = Session(engine)
    seed_pricing_table(s)
    return s


def _chatgpt_event(
    session: Session,
    *,
    event_id: str = "ev_001",
    model_id: str = "gpt-5.4-mini",
    ts: datetime = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
    cost_usd: float = 0.0,
    tokens_input: int = 1_000_000,
    tokens_output: int = 1_000_000,
) -> UsageEvent:
    ev = UsageEvent(
        provider_id="chatgpt",
        account_id="user@test.com",
        event_id=event_id,
        ts=ts,
        kind="message",
        model_id=model_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
    )
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return ev


# ---------------------------------------------------------------------------
# Phase B — re-cost usage_events
# ---------------------------------------------------------------------------


def test_phase_b_updates_zero_cost_event():
    s = _make_session()
    ev = _chatgpt_event(s, cost_usd=0.0)

    updated, unchanged, zeroed = phase_b_recost(s, providers=["chatgpt"], since=None, dry_run=False)

    s.refresh(ev)
    # gpt-5.4-mini: $0.75 + $4.50 = $5.25 per M tokens
    assert ev.cost_usd == 5.25
    assert updated == 1
    assert unchanged == 0
    assert zeroed == 0


def test_phase_b_skips_opencode_events():
    s = _make_session()
    oc_ev = UsageEvent(
        provider_id="opencode",
        account_id="user@test.com",
        event_id="oc_001",
        ts=datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        kind="message",
        model_id="gpt-5.4-mini",
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        cost_usd=99.0,
    )
    s.add(oc_ev)
    s.commit()

    phase_b_recost(s, providers=None, since=None, dry_run=False)

    s.refresh(oc_ev)
    assert oc_ev.cost_usd == 99.0  # untouched


def test_phase_b_skips_error_events():
    s = _make_session()
    err_ev = UsageEvent(
        provider_id="chatgpt",
        account_id="user@test.com",
        event_id="err_001",
        ts=datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        kind="error",
        model_id="gpt-5.4-mini",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0.0,
    )
    s.add(err_ev)
    s.commit()

    updated, unchanged, zeroed = phase_b_recost(s, providers=["chatgpt"], since=None, dry_run=False)
    assert updated == 0
    assert unchanged == 0
    assert zeroed == 0


def test_phase_b_dry_run_does_not_write():
    s = _make_session()
    ev = _chatgpt_event(s, cost_usd=0.0)

    phase_b_recost(s, providers=["chatgpt"], since=None, dry_run=True)

    s.refresh(ev)
    assert ev.cost_usd == 0.0  # unchanged in DB


def test_phase_b_unchanged_count_when_cost_already_correct():
    s = _make_session()
    ev = _chatgpt_event(s, cost_usd=5.25)  # already the right value

    updated, unchanged, zeroed = phase_b_recost(s, providers=["chatgpt"], since=None, dry_run=False)
    assert updated == 0
    assert unchanged == 1


def test_phase_b_since_filter_skips_old_events():
    s = _make_session()
    old_ev = _chatgpt_event(s, event_id="old", ts=datetime(2025, 9, 1, tzinfo=UTC), cost_usd=0.0)
    new_ev = _chatgpt_event(s, event_id="new", ts=datetime(2026, 5, 16, tzinfo=UTC), cost_usd=0.0)

    from datetime import date

    phase_b_recost(s, providers=["chatgpt"], since=date(2026, 1, 1), dry_run=False)

    s.refresh(old_ev)
    s.refresh(new_ev)
    assert old_ev.cost_usd == 0.0  # before since — untouched
    assert new_ev.cost_usd == 5.25  # after since — updated


# ---------------------------------------------------------------------------
# Phase C — rebuild rollups
# ---------------------------------------------------------------------------


def test_phase_c_rebuilds_rollup_with_new_cost():
    s = _make_session()
    # Insert event with correct cost, then corrupt the rollup to simulate stale state.
    ev = _chatgpt_event(s, cost_usd=5.25)
    from app.services.period_rollups import update_rollups_for_event

    update_rollups_for_event(s, ev)
    rollup = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "chatgpt",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert rollup is not None
    rollup.cost_usd = 0.0
    s.add(rollup)
    s.commit()

    phase_c_rollups(s, providers=["chatgpt"], dry_run=False)

    # Re-query — phase_c deletes and recreates the row so the old reference is gone.
    rebuilt = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "chatgpt",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert rebuilt is not None
    assert rebuilt.cost_usd == 5.25


def test_phase_c_dry_run_leaves_rollup_intact():
    s = _make_session()
    ev = _chatgpt_event(s, cost_usd=5.25)
    from app.services.period_rollups import update_rollups_for_event

    update_rollups_for_event(s, ev)

    phase_c_rollups(s, providers=["chatgpt"], dry_run=True)

    rows = s.exec(select(UsagePeriodRollup).where(UsagePeriodRollup.provider_id == "chatgpt")).all()
    assert len(rows) > 0  # rollup still exists


# ---------------------------------------------------------------------------
# Phase D — rebuild windows
# ---------------------------------------------------------------------------


def test_phase_d_rebuilds_window_with_updated_event_cost():
    s = _make_session()
    ev = _chatgpt_event(s, cost_usd=5.25)

    # Create a closed window covering the event's timestamp
    ws = datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)
    we = datetime(2026, 5, 23, 0, 0, 0, tzinfo=UTC)
    close_window(
        s,
        provider_id="chatgpt",
        account_id="user@test.com",
        window_type="weekly",
        window_start=ws,
        window_end=we,
    )
    s.commit()

    # Now change the event cost and re-run phase D
    ev.cost_usd = 10.50
    s.add(ev)
    s.commit()

    phase_d_windows(s, providers=["chatgpt"], dry_run=False)

    window = s.exec(
        select(UsageWindow).where(
            UsageWindow.provider_id == "chatgpt",
            UsageWindow.window_type == "weekly",
            UsageWindow.model_id == "",
            UsageWindow.sidecar_id == "",
        )
    ).first()
    assert window is not None
    assert window.cost_usd == 10.50


def test_phase_d_dry_run_does_not_delete_windows():
    s = _make_session()
    _chatgpt_event(s, cost_usd=5.25)
    ws = datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)
    we = datetime(2026, 5, 23, 0, 0, 0, tzinfo=UTC)
    close_window(
        s,
        provider_id="chatgpt",
        account_id="user@test.com",
        window_type="weekly",
        window_start=ws,
        window_end=we,
    )
    s.commit()

    phase_d_windows(s, providers=["chatgpt"], dry_run=True)

    count = len(s.exec(select(UsageWindow).where(UsageWindow.provider_id == "chatgpt")).all())
    assert count > 0  # windows still present
