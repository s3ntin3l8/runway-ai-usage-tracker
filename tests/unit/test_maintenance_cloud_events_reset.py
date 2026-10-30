"""Tests for app/services/maintenance/cloud_events_reset.py and the scripts that run it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import scripts.remove_cloud_coarse_events as coarse_script
import scripts.reset_cloud_events as reset_script
from app.models.db import (
    ClaudeCloudSessionCheckpoint,
    UsageEvent,
    UsagePeriodRollup,
    UsageWindow,
)
from app.services import claude_cloud_collector as collector
from app.services.maintenance.cloud_events_reset import (
    apply_cloud_events_reset,
    cloud_event_where,
    plan_cloud_events_reset,
)
from app.services.period_rollups import rebuild_rollups_for_pairs

DIGEST = "a" * 32
ACCOUNT = "holder@example.com"


def _engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _session() -> Session:
    return Session(_engine())


def _event(session: Session, event_id: str, **overrides) -> UsageEvent:
    base = {
        "provider_id": "anthropic",
        "account_id": ACCOUNT,
        "sidecar_id": "server",
        "event_id": event_id,
        "ts": datetime(2026, 9, 24, 12, tzinfo=UTC),
        "kind": "message",
        "model_id": "opus-5.5",
        "session_id": DIGEST,
        "entrypoint": "cloud",
        "tokens_input": 10,
        "tokens_output": 20,
        "tokens_cache_read": 300,
        "tokens_cache_create": 40,
        "cost_usd": 0.5,
    }
    base.update(overrides)
    row = UsageEvent(**base)
    session.add(row)
    session.commit()
    return row


def _seed(session: Session) -> None:
    _event(session, f"cloud:{DIGEST}:m:msg_1")
    _event(session, f"cloud:{DIGEST}:m:msg_2")
    _event(session, f"cloud:{DIGEST}:r:1:opus-5.5")
    _event(session, f"cloud:{'b' * 32}:m:msg_1", session_id="b" * 32)
    # Not reset: a first-collector coarse event, a local event, another provider's lookalike.
    _event(session, f"cloud:{DIGEST}:1")
    _event(session, "msg_local_1", entrypoint="cli", sidecar_id="dev-01", session_id="local-1")
    _event(session, f"cloud:{DIGEST}:m:msg_9", provider_id="openai")
    session.add(ClaudeCloudSessionCheckpoint(account_id=ACCOUNT, session_digest=DIGEST))
    session.add(ClaudeCloudSessionCheckpoint(account_id=ACCOUNT, session_digest="b" * 32))
    session.commit()
    rebuild_rollups_for_pairs(session, {("anthropic", ACCOUNT), ("openai", ACCOUNT)})
    session.commit()


def _lifetime(session: Session) -> UsagePeriodRollup:
    return session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "anthropic",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).one()


def test_plan_counts_the_history_events_and_checkpoints():
    session = _session()
    _seed(session)
    plan = plan_cloud_events_reset(session)
    assert (plan.events, plan.sessions, plan.checkpoints) == (4, 2, 2)
    assert plan.sidecar_ids == ["server"] and plan.accounts == [ACCOUNT]
    assert len(plan.samples) == 4 and all(":m:" in s or ":r:" in s for s in plan.samples)


def test_plan_is_read_only():
    session = _session()
    _seed(session)
    plan_cloud_events_reset(session)
    assert len(session.exec(select(UsageEvent)).all()) == 7
    assert len(session.exec(select(ClaudeCloudSessionCheckpoint)).all()) == 2


def test_apply_removes_events_and_checkpoints_and_repairs_the_rollups():
    session = _session()
    _seed(session)
    result = apply_cloud_events_reset(session)
    assert (result.events_deleted, result.checkpoints_deleted) == (4, 2)
    remaining = {e.event_id for e in session.exec(select(UsageEvent))}
    assert remaining == {f"cloud:{DIGEST}:1", "msg_local_1", f"cloud:{DIGEST}:m:msg_9"}
    assert session.exec(select(ClaudeCloudSessionCheckpoint)).all() == []
    assert _lifetime(session).msgs == 2  # the coarse event and the local event stay


def test_apply_is_idempotent():
    session = _session()
    _seed(session)
    apply_cloud_events_reset(session)
    again = apply_cloud_events_reset(session)
    assert (again.events_deleted, again.checkpoints_deleted) == (0, 0)
    assert len(session.exec(select(UsageEvent)).all()) == 3


def test_apply_clears_the_collectors_in_memory_backlog():
    session = _session()
    _seed(session)
    collector._PENDING["holder"] = []
    try:
        apply_cloud_events_reset(session)
        assert collector._PENDING == {}
    finally:
        collector._PENDING.clear()


def test_the_reset_does_not_make_the_migration_guard_pending(monkeypatch):
    # Only the first collector's events block the collector; the reset must not create any.
    session = _session()
    _seed(session)
    session.exec(UsageEvent.__table__.delete().where(UsageEvent.event_id == f"cloud:{DIGEST}:1"))  # type: ignore[arg-type]
    session.commit()
    monkeypatch.setattr(collector, "engine", session.get_bind())
    assert collector._migration_pending() is False
    apply_cloud_events_reset(session)
    assert collector._migration_pending() is False


def _closed_window(session: Session, **overrides) -> UsageWindow:
    base = {
        "provider_id": "anthropic",
        "account_id": ACCOUNT,
        "window_type": "weekly",
        "window_start": datetime(2026, 9, 21, tzinfo=UTC),
        "window_end": datetime(2026, 9, 28, tzinfo=UTC),
        "model_id": "",
        "sidecar_id": "",
        "msgs": 7,
        "tokens_output": 140,
        "limit_value": 100.0,
        "pct_used": 42.0,
    }
    base.update(overrides)
    window = UsageWindow(**base)
    session.add(window)
    session.commit()
    return window


def test_a_closed_window_follows_the_remaining_events_and_keeps_its_gauge():
    session = _session()
    _seed(session)
    _closed_window(session)
    result = apply_cloud_events_reset(session)
    assert result.windows_rebuilt >= 1
    window = session.exec(
        select(UsageWindow).where(UsageWindow.model_id == "", UsageWindow.sidecar_id == "")
    ).one()
    assert window.msgs == 2 and window.limit_value == 100.0 and window.pct_used == 42.0


def test_a_window_outside_the_removed_range_is_not_rebuilt():
    session = _session()
    _seed(session)
    _closed_window(
        session,
        window_start=datetime(2026, 3, 2, tzinfo=UTC),
        window_end=datetime(2026, 3, 9, tzinfo=UTC),
        msgs=900,
    )
    apply_cloud_events_reset(session)
    assert session.exec(select(UsageWindow)).one().msgs == 900


def test_a_replayed_window_range_rebuilds_windows_after_an_interrupted_run():
    session = _session()
    _seed(session)
    _closed_window(session)
    # Simulate the crash: events and checkpoints are gone, the rollups and the window still count them.
    for row in session.exec(select(UsageEvent).where(*cloud_event_where())).all():
        session.delete(row)
    session.commit()
    untouched = apply_cloud_events_reset(session)
    assert untouched.windows_rebuilt == 0
    assert session.exec(select(UsageWindow)).one().msgs == 7
    replayed = apply_cloud_events_reset(
        session,
        windows_range=(datetime(2026, 9, 24, tzinfo=UTC), datetime(2026, 9, 25, tzinfo=UTC)),
    )
    assert replayed.windows_rebuilt >= 1
    window = session.exec(
        select(UsageWindow).where(UsageWindow.model_id == "", UsageWindow.sidecar_id == "")
    ).one()
    assert window.msgs == 2


# -- the scripts ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "seed_id", "left"),
    [
        (reset_script, f"cloud:{DIGEST}:m:msg_1", 0),
        (coarse_script, f"cloud:{DIGEST}:1", 0),
    ],
)
def test_the_scripts_run_with_apply_and_the_window_flags(
    monkeypatch, capsys, module, seed_id, left
):
    engine = _engine()
    with Session(engine) as session:
        _event(session, seed_id)
    monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(module, "init_db", lambda: None)
    module.main(["--apply"])
    module.main(
        [
            "--apply",
            "--windows-from",
            "2026-09-24T00:00:00+00:00",
            "--windows-to",
            "2026-09-25T00:00:00+00:00",
        ]
    )
    with Session(engine) as session:
        assert len(session.exec(select(UsageEvent)).all()) == left
    assert "Done:" in capsys.readouterr().out


def test_the_report_only_run_changes_nothing(monkeypatch, capsys):
    module = reset_script
    engine = _engine()
    with Session(engine) as session:
        _event(session, f"cloud:{DIGEST}:m:msg_1")
    monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(module, "init_db", lambda: None)
    module.main([])
    with Session(engine) as session:
        assert len(session.exec(select(UsageEvent)).all()) == 1
    assert "Report only" in capsys.readouterr().out
