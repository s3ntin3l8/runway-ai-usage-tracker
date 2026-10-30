"""The cleanup_antigravity recost phase shares phase_b_recost with
recost_events.py; an arity change there must fail a test, not a live run."""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import ProviderPricing, UsageEvent
from scripts import cleanup_antigravity


def test_apply_runs_the_recost_phases_end_to_end(monkeypatch, capsys):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(
            ProviderPricing(
                provider_id="antigravity",
                model_id="gemini-test",
                effective_from=date(2020, 1, 1),
                input_per_mtok=2.0,
                output_per_mtok=2.0,
                cache_read_per_mtok=0.0,
                cache_create_per_mtok=0.0,
            )
        )
        s.add(
            UsageEvent(
                provider_id="antigravity",
                account_id="default",
                sidecar_id="dev-01",
                event_id="ag-1",
                ts=datetime(2026, 9, 1, tzinfo=UTC),
                kind="message",
                model_id="gemini-test",
                tokens_input=1_000_000,
                cost_usd=0.0,
            )
        )
        s.commit()
    monkeypatch.setattr(cleanup_antigravity, "engine", engine)
    monkeypatch.setattr(
        sys, "argv", ["cleanup_antigravity.py", "--apply", "--email", "a@example.com"]
    )

    assert cleanup_antigravity.main() == 0

    out = capsys.readouterr().out
    assert "Phase C-events: 1 total" in out
    assert "Phase C-rollups" in out and "Phase C-windows" in out
    with Session(engine) as s:
        ev = s.exec(select(UsageEvent)).one()
        assert ev.cost_usd == 2.0 and ev.account_id == "a@example.com"
