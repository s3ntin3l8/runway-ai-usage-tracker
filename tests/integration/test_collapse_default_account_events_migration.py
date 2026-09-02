"""Integration test for scripts/collapse_default_account_events.py.

Seeds usage_events reproducing the patterns found in production (duplicate
pairs, a lone "default" row, an unresolvable multi-account provider), runs
the migration, and asserts the winner survives, the loser is deleted, the
account_id is normalized, and rollups are rebuilt.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.pricing_seed import seed_pricing_table

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


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
    with Session(eng) as s:
        seed_pricing_table(s)
        s.commit()
    return eng


def _ev(provider_id: str, account_id: str, event_id: str, **kw) -> UsageEvent:
    return UsageEvent(
        provider_id=provider_id,
        account_id=account_id,
        sidecar_id="local",
        event_id=event_id,
        ts=kw.pop("ts", NOW),
        kind=kw.pop("kind", "message"),
        model_id=kw.pop("model_id", "some-model"),
        tokens_input=kw.pop("tokens_input", 100),
        tokens_output=kw.pop("tokens_output", 50),
        cost_usd=kw.pop("cost_usd", 0.0),
    )


def test_migration_collapses_duplicates_and_retags_lone_rows(engine):
    with Session(engine) as s:
        # True duplicate pair — lower id wins (deterministic tie-break).
        s.add(_ev("antigravity", "default", "e_dup", tokens_input=500, tokens_output=100))
        s.add(_ev("antigravity", "user@example.com", "e_dup", tokens_input=500, tokens_output=100))
        # Divergent pair — larger tokens wins, even tagged "default".
        s.add(_ev("antigravity", "user@example.com", "e_grow", tokens_input=100, tokens_output=10))
        s.add(_ev("antigravity", "default", "e_grow", tokens_input=9000, tokens_output=400))
        # kind-diverging pair — "message" wins over "error" regardless of tokens.
        s.add(
            _ev(
                "antigravity",
                "default",
                "e_err",
                kind="error",
                tokens_input=0,
                tokens_output=0,
            )
        )
        s.add(
            _ev(
                "antigravity",
                "user@example.com",
                "e_err",
                kind="message",
                tokens_input=0,
                tokens_output=0,
            )
        )
        # Lone "default" row, no duplicate — must be retagged, not deleted.
        s.add(_ev("antigravity", "default", "e_lone", tokens_input=42, tokens_output=7))
        s.commit()

    with (
        patch("scripts.collapse_default_account_events.engine", engine),
        patch("scripts.backfill_rollups.engine", engine),
    ):
        from scripts.collapse_default_account_events import migrate

        stats = migrate("antigravity", "default", None, apply=True)

    assert stats == {"pairs": 3, "lone_retagged": 1, "deleted": 3, "retagged": 2}

    with Session(engine) as s:
        rows = s.exec(select(UsageEvent)).all()
        by_event = {r.event_id: r for r in rows}
        assert len(rows) == 4  # 3 pairs -> 3 survivors + 1 lone = 4, all under the real email

        assert by_event["e_dup"].account_id == "user@example.com"
        assert by_event["e_dup"].tokens_input == 500

        assert by_event["e_grow"].account_id == "user@example.com"
        assert by_event["e_grow"].tokens_input == 9000  # the larger (originally "default") copy

        assert by_event["e_err"].account_id == "user@example.com"
        assert by_event["e_err"].kind == "message"

        assert by_event["e_lone"].account_id == "user@example.com"

        # Rollup rebuilt for the provider, keyed on the now-canonical account.
        rollup = s.exec(
            select(UsagePeriodRollup).where(UsagePeriodRollup.provider_id == "antigravity")
        ).first()
        assert rollup is not None
        assert rollup.account_id == "user@example.com"


def test_migration_aborts_when_target_ambiguous(engine):
    """More than one non-'default' account_id present -> refuse to guess."""
    with Session(engine) as s:
        s.add(_ev("chatgpt", "default", "e1"))
        s.add(_ev("chatgpt", "a@example.com", "e2"))
        s.add(_ev("chatgpt", "b@example.com", "e3"))
        s.commit()

    with patch("scripts.collapse_default_account_events.engine", engine):
        from scripts.collapse_default_account_events import migrate

        stats = migrate("chatgpt", "default", None, apply=True)

    assert stats == {"pairs": 0, "lone_retagged": 0, "deleted": 0, "retagged": 0}
    with Session(engine) as s:
        # Nothing touched.
        rows = {r.event_id: r.account_id for r in s.exec(select(UsageEvent)).all()}
        assert rows == {"e1": "default", "e2": "a@example.com", "e3": "b@example.com"}


def test_migration_explicit_account_id_overrides_autodetect(engine):
    with Session(engine) as s:
        s.add(_ev("chatgpt", "default", "e1"))
        s.add(_ev("chatgpt", "a@example.com", "e2"))
        s.add(_ev("chatgpt", "b@example.com", "e3"))
        s.commit()

    with (
        patch("scripts.collapse_default_account_events.engine", engine),
        patch("scripts.backfill_rollups.engine", engine),
    ):
        from scripts.collapse_default_account_events import migrate

        stats = migrate("chatgpt", "default", "a@example.com", apply=True)

    assert stats["lone_retagged"] == 1
    with Session(engine) as s:
        assert s.exec(select(UsageEvent).where(UsageEvent.event_id == "e1")).one().account_id == (
            "a@example.com"
        )


def test_migration_dry_run_writes_nothing(engine):
    with Session(engine) as s:
        # A true tie (equal tokens) falls to the id tie-break — "default" was
        # added first (lower id) so it wins and would need retagging.
        s.add(_ev("antigravity", "default", "e_dup", tokens_input=1))
        s.add(_ev("antigravity", "user@example.com", "e_dup", tokens_input=1))
        s.commit()

    with patch("scripts.collapse_default_account_events.engine", engine):
        from scripts.collapse_default_account_events import migrate

        stats = migrate("antigravity", "default", None, apply=False)

    # Dry-run must report the same counts an --apply run would (previously
    # deleted/retagged stayed 0 in dry-run even though the per-pair log lines
    # enumerated them, which read as "would apply, but nothing found to do").
    assert stats == {"pairs": 1, "lone_retagged": 0, "deleted": 1, "retagged": 1}
    with Session(engine) as s:
        rows = s.exec(select(UsageEvent)).all()
        assert len(rows) == 2  # nothing actually deleted
        assert {r.account_id for r in rows} == {
            "default",
            "user@example.com",
        }  # nothing actually retagged
