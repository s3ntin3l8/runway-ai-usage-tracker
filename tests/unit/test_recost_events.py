"""Unit tests for the recost_events backfill script."""

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup, UsageWindow
from app.services.period_rollups import update_rollups_for_event
from app.services.pricing_seed import seed_pricing_table
from app.services.window_closer import close_window
from scripts.recost_events import phase_b_recost, phase_c_rollups, phase_d_windows, run

# Most of these tests exercise Phase B → Phase C/D directly (pass Phase B's
# affected_pairs into Phase C/D) rather than calling `run()` — `run()` opens
# its own Session against the module-level `app.core.db.engine`, not the
# in-memory test engine `_make_session()` builds. The one test that needs to
# exercise `run()`'s own skip-when-empty gate monkeypatches
# `scripts.recost_events.engine` to the test engine instead; that works
# because `_make_session()` uses a `StaticPool`, so every Session bound to
# that engine shares the same underlying connection.


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


def _gemini_event_unchanged(
    session: Session,
    *,
    event_id: str = "gm_001",
    ts: datetime = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
) -> UsageEvent:
    """A gemini event with zero tokens, so its recomputed cost is 0.0 — the
    same as the field defaults it's created with. Phase B always classifies
    it "unchanged", so its (provider_id, account_id) pair never lands in
    affected_pairs regardless of what pricing changes for other providers."""
    ev = UsageEvent(
        provider_id="gemini",
        account_id="gem-user@test.com",
        event_id=event_id,
        ts=ts,
        kind="message",
        model_id="gemini-2.5-pro",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0.0,
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

    updated, unchanged, zeroed, affected_pairs = phase_b_recost(
        s, providers=["chatgpt"], since=None, dry_run=False
    )

    s.refresh(ev)
    # gpt-5.4-mini: $0.75 + $4.50 = $5.25 per M tokens
    assert ev.cost_usd == 5.25
    assert updated == 1
    assert unchanged == 0
    assert zeroed == 0
    assert affected_pairs == {("chatgpt", "user@test.com")}


def test_phase_b_preserves_unknown_opencode_total_and_reported_cost():
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
    assert oc_ev.cost_usd == 99.0
    assert oc_ev.cost_reported_usd == 99.0
    assert oc_ev.cost_estimated_usd == 0.0


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

    updated, unchanged, zeroed, affected_pairs = phase_b_recost(
        s, providers=["chatgpt"], since=None, dry_run=False
    )
    assert updated == 0
    assert unchanged == 0
    assert zeroed == 0
    assert affected_pairs == set()


def test_phase_b_dry_run_does_not_write():
    s = _make_session()
    ev = _chatgpt_event(s, cost_usd=0.0)

    phase_b_recost(s, providers=["chatgpt"], since=None, dry_run=True)

    s.refresh(ev)
    assert ev.cost_usd == 0.0  # unchanged in DB


def test_phase_b_unchanged_count_when_cost_already_correct():
    s = _make_session()
    ev = _chatgpt_event(s, cost_usd=5.25)
    ev.cost_estimated_usd = 5.25
    s.add(ev)
    s.commit()

    updated, unchanged, zeroed, affected_pairs = phase_b_recost(
        s, providers=["chatgpt"], since=None, dry_run=False
    )
    assert updated == 0
    assert unchanged == 1
    assert affected_pairs == set()


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


# ---------------------------------------------------------------------------
# Phase B → Phase C/D chaining — only rebuilds the pairs Phase B actually
# changed (issue #373)
# ---------------------------------------------------------------------------


def test_affected_pairs_narrows_rebuild_to_the_provider_that_changed():
    s = _make_session()

    # chatgpt: a real price change (starts at the wrong cost_usd).
    chatgpt_ev = _chatgpt_event(s, cost_usd=0.0)
    update_rollups_for_event(s, chatgpt_ev)

    # gemini: unrelated provider, nothing to recost.
    gemini_ev = _gemini_event_unchanged(s)
    update_rollups_for_event(s, gemini_ev)

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
    close_window(
        s,
        provider_id="gemini",
        account_id="gem-user@test.com",
        window_type="daily",
        window_start=ws,
        window_end=we,
    )
    s.commit()

    def _rollup_row(provider_id: str) -> UsagePeriodRollup:
        row = s.exec(
            select(UsagePeriodRollup).where(
                UsagePeriodRollup.provider_id == provider_id,
                UsagePeriodRollup.period_type == "lifetime",
                UsagePeriodRollup.model_id == "",
                UsagePeriodRollup.sidecar_id == "",
            )
        ).first()
        assert row is not None
        return row

    def _window_row(provider_id: str, window_type: str) -> UsageWindow:
        row = s.exec(
            select(UsageWindow).where(
                UsageWindow.provider_id == provider_id,
                UsageWindow.window_type == window_type,
                UsageWindow.model_id == "",
                UsageWindow.sidecar_id == "",
            )
        ).first()
        assert row is not None
        return row

    # Sentinel values on the gemini rows: nothing in this test's event data
    # would ever recompute to 123.0, so if a "rebuild" touches these rows at
    # all — even a delete+recreate with the same id (SQLite can reuse
    # rowids) — the sentinel gets overwritten and the assertions below catch
    # it. Checking `id`/`last_updated` alone wouldn't.
    gemini_rollup_before = _rollup_row("gemini")
    gemini_rollup_before.cost_usd = 123.0
    s.add(gemini_rollup_before)
    gemini_window_before = _window_row("gemini", "daily")
    gemini_window_before.cost_usd = 123.0
    s.add(gemini_window_before)
    s.commit()
    gemini_rollup_id, gemini_rollup_last_updated = (
        gemini_rollup_before.id,
        gemini_rollup_before.last_updated,
    )
    gemini_window_id = gemini_window_before.id

    # Same sequencing run() uses: Phase B, then thread its affected_pairs
    # into Phase C/D — with `--all` (providers=None) at the CLI.
    _updated, _unchanged, _zeroed, affected_pairs = phase_b_recost(
        s, providers=None, since=None, dry_run=False
    )
    assert affected_pairs == {("chatgpt", "user@test.com")}
    assert affected_pairs  # run() would not skip Phase C/D here
    phase_c_rollups(s, providers=None, dry_run=False, pairs=affected_pairs)
    phase_d_windows(s, providers=None, dry_run=False, pairs=affected_pairs)

    # rebuild_windows_for_providers expunges the session's identity map, so
    # re-query rather than refresh the stale `chatgpt_ev` instance.
    chatgpt_ev_after = s.exec(select(UsageEvent).where(UsageEvent.event_id == "ev_001")).first()
    assert chatgpt_ev_after is not None
    assert chatgpt_ev_after.cost_usd == 5.25  # chatgpt actually recost

    chatgpt_rollup_after = _rollup_row("chatgpt")
    assert chatgpt_rollup_after.cost_usd == 5.25  # rollup rebuilt with new cost

    chatgpt_window_after = _window_row("chatgpt", "weekly")
    assert chatgpt_window_after.cost_usd == 5.25  # window rebuilt with new cost

    # gemini was never touched — sentinel cost_usd survives, and it's the
    # exact same row (same id, same last_updated), not merely the same
    # values from a delete+recreate.
    gemini_rollup_after = _rollup_row("gemini")
    assert gemini_rollup_after.cost_usd == 123.0
    assert gemini_rollup_after.id == gemini_rollup_id
    assert gemini_rollup_after.last_updated == gemini_rollup_last_updated

    gemini_window_after = _window_row("gemini", "daily")
    assert gemini_window_after.cost_usd == 123.0
    assert gemini_window_after.id == gemini_window_id


def test_run_skips_phase_c_and_d_when_no_cost_changes(monkeypatch, capsys):
    """run() itself — not just the phase functions — must gate Phase C/D on
    affected_pairs, printing the skip message and touching neither table."""
    s = _make_session()

    # gemini event whose recomputed cost equals what's already stored — no
    # cost change anywhere in scope.
    gemini_ev = _gemini_event_unchanged(s)
    update_rollups_for_event(s, gemini_ev)

    ws = datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)
    we = datetime(2026, 5, 23, 0, 0, 0, tzinfo=UTC)
    close_window(
        s,
        provider_id="gemini",
        account_id="gem-user@test.com",
        window_type="daily",
        window_start=ws,
        window_end=we,
    )
    s.commit()

    rollup_before = s.exec(
        select(UsagePeriodRollup).where(UsagePeriodRollup.provider_id == "gemini")
    ).first()
    assert rollup_before is not None
    rollup_id, rollup_last_updated = rollup_before.id, rollup_before.last_updated

    window_before = s.exec(select(UsageWindow).where(UsageWindow.provider_id == "gemini")).first()
    assert window_before is not None
    window_id = window_before.id

    # Point run()'s module-level `engine` at this test's in-memory engine —
    # a StaticPool, so run()'s own `Session(engine)` shares the same
    # underlying connection `s` is using.
    monkeypatch.setattr("scripts.recost_events.engine", s.get_bind())

    run(providers=None, since=None, dry_run=False, skip_rollups=False, skip_windows=False)

    out = capsys.readouterr().out
    assert "No cost changes — Phases C/D skipped" in out
    assert "0 pair(s) affected: 0 event(s), 0 window(s) would be rebuilt" in out
    assert "Phase C —" not in out
    assert "Phase D —" not in out

    rollup_after = s.exec(
        select(UsagePeriodRollup).where(UsagePeriodRollup.provider_id == "gemini")
    ).first()
    assert rollup_after is not None
    assert rollup_after.id == rollup_id
    assert rollup_after.last_updated == rollup_last_updated

    window_after = s.exec(select(UsageWindow).where(UsageWindow.provider_id == "gemini")).first()
    assert window_after is not None
    assert window_after.id == window_id


def test_phase_c_and_d_given_empty_pairs_directly_are_a_no_op():
    """Even called directly (not via run()'s skip-before-calling gate), an
    explicit empty pairs set must not fall through to the "empty list means
    every provider" behavior `providers=[]` has."""
    s = _make_session()
    ev = _chatgpt_event(s, cost_usd=5.25)
    update_rollups_for_event(s, ev)
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

    rollup_before = s.exec(
        select(UsagePeriodRollup).where(UsagePeriodRollup.provider_id == "chatgpt")
    ).first()
    assert rollup_before is not None
    rollup_id = rollup_before.id
    window_before = s.exec(select(UsageWindow).where(UsageWindow.provider_id == "chatgpt")).first()
    assert window_before is not None
    window_id = window_before.id

    n_events = phase_c_rollups(s, providers=["chatgpt"], dry_run=False, pairs=set())
    n_windows = phase_d_windows(s, providers=["chatgpt"], dry_run=False, pairs=set())
    assert n_events == 0
    assert n_windows == 0

    rollup_after = s.exec(
        select(UsagePeriodRollup).where(UsagePeriodRollup.provider_id == "chatgpt")
    ).first()
    assert rollup_after is not None
    assert rollup_after.id == rollup_id

    window_after = s.exec(select(UsageWindow).where(UsageWindow.provider_id == "chatgpt")).first()
    assert window_after is not None
    assert window_after.id == window_id
