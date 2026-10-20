"""Unit tests for the backfill_argon_models script's CLI.

Mirrors tests/unit/test_recost_events.py: the service-level behaviour is
covered in test_argon_pricing.py; these drive the script's own `run()` /
`main()` so the dry-run wording and the `--provider` / `--skip-*` wiring stay
guarded.

`run()` opens its own Session against the module-level `app.core.db.engine`,
so the tests that need it monkeypatch `scripts.backfill_argon_models.engine`
to the in-memory test engine. That works because the test engine uses a
`StaticPool`, so every Session bound to it shares one connection.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.period_rollups import rebuild_rollups_for_pairs
from app.services.pricing_seed import seed_pricing_table
from scripts.backfill_argon_models import main, run


def _make_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    s = Session(engine)
    seed_pricing_table(s)
    return s


def _argon_event(session: Session, **overrides) -> UsageEvent:
    base = {
        "provider_id": "antigravity",
        "account_id": "alice@example.com",
        "event_id": "conv-1|gen_0",
        "ts": datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
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


def _run(monkeypatch, session: Session, **kwargs) -> None:
    monkeypatch.setattr("scripts.backfill_argon_models.engine", session.get_bind())
    _call_run(dry_run=False, **kwargs)


def _call_run(*, dry_run: bool, providers=None, skip_rollups=False, skip_windows=False) -> None:
    run(
        providers=providers,
        dry_run=dry_run,
        skip_rollups=skip_rollups,
        skip_windows=skip_windows,
    )


def test_dry_run_reports_the_plan_without_writing(monkeypatch, capsys):
    session = _make_session()
    _argon_event(session)
    monkeypatch.setattr("scripts.backfill_argon_models.engine", session.get_bind())

    _call_run(dry_run=True)

    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "gemini-4-argon-trial" in out
    # A dry run writes nothing.
    ev = session.exec(select(UsageEvent)).first()
    assert ev is not None
    assert ev.model_id == "gemini-4-argon-trial"
    assert ev.cost_usd == 0.0


def test_dry_run_on_a_repaired_database_says_nothing_to_do(monkeypatch, capsys):
    session = _make_session()
    _argon_event(session)
    _run(monkeypatch, session)

    _call_run(dry_run=True)

    out = capsys.readouterr().out
    assert "no Argon model ids to repair" in out


def test_apply_rewrites_the_id_and_reprices(monkeypatch, capsys):
    session = _make_session()
    _argon_event(session)

    _run(monkeypatch, session)

    out = capsys.readouterr().out
    assert "rewrote 1 event(s)" in out
    ev = session.exec(select(UsageEvent)).first()
    assert ev is not None
    assert ev.model_id == "gemini-4-argon"
    assert ev.cost_usd == 12.0


def test_provider_scoping_leaves_other_providers_alone(monkeypatch):
    session = _make_session()
    _argon_event(session)
    _argon_event(
        session,
        provider_id="gemini",
        event_id="hermes|profile|sess|gemini-4-argon-trial|task|c1s1",
    )

    _run(monkeypatch, session, providers=["antigravity"])

    models = {(ev.provider_id, ev.model_id) for ev in session.exec(select(UsageEvent)).all()}
    assert models == {
        ("antigravity", "gemini-4-argon"),
        ("gemini", "gemini-4-argon-trial"),
    }


def test_skip_rollups_leaves_the_stale_grain_behind(monkeypatch):
    session = _make_session()
    _argon_event(session)
    rebuild_rollups_for_pairs(session, {("antigravity", "alice@example.com")})

    _run(monkeypatch, session, skip_rollups=True)

    grains = {row.model_id for row in session.exec(select(UsagePeriodRollup)).all()}
    assert "gemini-4-argon-trial" in grains


def test_main_accepts_the_documented_flags(monkeypatch, capsys):
    session = _make_session()
    _argon_event(session)
    monkeypatch.setattr("scripts.backfill_argon_models.engine", session.get_bind())

    assert main(["--provider", "antigravity", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "gemini-4-argon-trial" in out
