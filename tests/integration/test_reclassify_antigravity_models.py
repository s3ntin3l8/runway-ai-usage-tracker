"""Integration test for scripts/reclassify_antigravity_models.py.

Seeds a legacy coarse bucket row, stubs the on-disk re-parse, and asserts
event_id matching repairs model_id/effort while --dry-run writes nothing.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent
from app.models.schemas import UsageEventPush

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def mock_db_session():
    """Override the conftest autouse Session mock — this test needs a real DB."""
    yield


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _seed_event(session: Session, event_id: str, **kw) -> None:
    session.add(
        UsageEvent(
            provider_id="antigravity",
            account_id="backfill",
            sidecar_id="local",
            event_id=event_id,
            ts=NOW,
            kind="message",
            model_id=kw.pop("model_id", "flash-3"),
            effort=kw.pop("effort", None),
            tokens_input=kw.pop("tokens_input", 100),
            tokens_output=kw.pop("tokens_output", 50),
            cost_usd=kw.pop("cost_usd", 0.0),
        )
    )


def _push(event_id: str, model_id: str, effort: str | None) -> UsageEventPush:
    return UsageEventPush(
        provider_id="antigravity",
        account_id="backfill",
        event_id=event_id,
        ts=NOW.isoformat(),
        model_id=model_id,
        effort=effort,
        tokens_input=100,
        tokens_output=50,
    )


def test_reclassify_repairs_matching_event_ids(engine):
    from scripts.reclassify_antigravity_models import reclassify

    with Session(engine) as s:
        _seed_event(s, "conv1|gen_0", model_id="flash-3", effort=None)
        _seed_event(s, "conv1|gen_1", model_id="flash-3", effort="high")  # already correct
        _seed_event(s, "missing|gen_0", model_id="pro-3")
        s.commit()

    pushes = {
        "conv1|gen_0": _push("conv1|gen_0", "flash-3.7", "medium"),
        "conv1|gen_1": _push("conv1|gen_1", "flash-3", "high"),
    }
    with (
        patch("scripts.reclassify_antigravity_models._collect_pushes", return_value=pushes),
        Session(engine) as s,
    ):
        changed = reclassify(s, dry_run=False)
        s.commit()

    assert changed == 1
    with Session(engine) as s:
        rows = {e.event_id: e for e in s.exec(select(UsageEvent)).all()}
    assert rows["conv1|gen_0"].model_id == "flash-3.7"
    assert rows["conv1|gen_0"].effort == "medium"
    assert rows["conv1|gen_1"].model_id == "flash-3"
    assert rows["conv1|gen_1"].effort == "high"
    # Source missing → left alone.
    assert rows["missing|gen_0"].model_id == "pro-3"


def test_reclassify_dry_run_writes_nothing(engine):
    from scripts.reclassify_antigravity_models import reclassify

    with Session(engine) as s:
        _seed_event(s, "conv1|gen_0", model_id="flash-3", effort=None)
        s.commit()

    pushes = {"conv1|gen_0": _push("conv1|gen_0", "flash-3.5", "low")}
    with (
        patch("scripts.reclassify_antigravity_models._collect_pushes", return_value=pushes),
        Session(engine) as s,
    ):
        changed = reclassify(s, dry_run=True)
        # dry_run must not commit; expire and re-read from DB.
        s.rollback()

    assert changed == 1
    with Session(engine) as s:
        row = s.exec(select(UsageEvent).where(UsageEvent.event_id == "conv1|gen_0")).one()
    assert row.model_id == "flash-3"
    assert row.effort is None
