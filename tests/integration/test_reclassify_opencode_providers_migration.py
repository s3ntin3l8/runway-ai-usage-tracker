"""Integration test for scripts/reclassify_opencode_providers.py.

Seeds usage_events under the old buggy provider_ids ("opencode",
"opencode-free" — issue #182) plus a local opencode.db carrying the real
providerID/error data, runs the migration, and asserts each event lands on
its correct sibling provider, failed requests become kind="error" with
zeroed usage, and rollups are rebuilt for every touched provider.
"""

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.pricing_seed import seed_pricing_table

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def mock_db_session():
    """Override the conftest autouse Session mock — this test needs a real DB.

    The migration (and scripts.backfill_rollups, which it calls) construct
    their own ``Session(engine)``, so the global ``sqlmodel.Session`` patch
    would otherwise hand them a no-op mock.
    """
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


def _seed_event(session: Session, event_id: str, provider_id: str, **kw) -> None:
    session.add(
        UsageEvent(
            provider_id=provider_id,
            account_id="default",
            sidecar_id="local",
            event_id=event_id,
            ts=NOW,
            kind="message",
            model_id=kw.pop("model_id", "some-model"),
            tokens_input=kw.pop("tokens_input", 100),
            tokens_output=kw.pop("tokens_output", 50),
            cost_usd=kw.pop("cost_usd", 0.0),
        )
    )


def _make_opencode_db(messages: list[dict]) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    for msg in messages:
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?,?,?,?,?)",
            (msg["id"], "ses1", 1778248860000, 1778248860000, json.dumps(msg["data"])),
        )
    conn.commit()
    conn.close()
    return db_path


def test_migration_reclassifies_byok_openrouter_and_errors(engine):
    # Seed events exactly as the old (buggy) extractor would have written them:
    # everything except free-tier collapsed onto "opencode" (Go).
    with Session(engine) as s:
        _seed_event(s, "msg_go", "opencode", tokens_input=500, tokens_output=100)
        _seed_event(s, "msg_byok", "opencode", tokens_input=999, tokens_output=999, cost_usd=0.0)
        _seed_event(s, "msg_openrouter_err", "opencode", tokens_input=1, tokens_output=1)
        _seed_event(s, "msg_free", "opencode-free", tokens_input=10, tokens_output=5)
        s.commit()

    db_path = _make_opencode_db(
        [
            {
                "id": "msg_go",
                "data": {
                    "role": "assistant",
                    "providerID": "opencode-go",
                    "modelID": "glm-5.1",
                    "cost": 0.01,
                    "tokens": {"input": 500, "output": 100},
                },
            },
            {
                "id": "msg_byok",
                "data": {
                    "role": "assistant",
                    "providerID": "open-design-byok",
                    "modelID": "tencent/hy3:free",
                    "cost": 0,
                    "tokens": {"input": 999, "output": 999},
                },
            },
            {
                "id": "msg_openrouter_err",
                "data": {
                    "role": "assistant",
                    "providerID": "openrouter",
                    "modelID": "google/gemma-4-31b-it:free",
                    "cost": 0,
                    "tokens": {"input": 1, "output": 1},
                    "error": {"name": "APIError", "data": {"statusCode": 401}},
                },
            },
            {
                "id": "msg_free",
                "data": {
                    "role": "assistant",
                    "providerID": "opencode",
                    "modelID": "minimax-m2.5-free",
                    "cost": 0,
                    "tokens": {"input": 10, "output": 5},
                },
            },
        ]
    )

    try:
        with (
            patch("scripts.reclassify_opencode_providers.engine", engine),
            patch("scripts.backfill_rollups.engine", engine),
        ):
            from scripts.reclassify_opencode_providers import migrate

            changed = migrate(db_path, apply=True)
    finally:
        db_path.unlink(missing_ok=True)

    # msg_go was already correctly tagged Go, and msg_free was already
    # correctly tagged opencode-free — only the 2 truly mislabeled rows
    # (byok, the failed openrouter request) actually change.
    assert changed == 2

    with Session(engine) as s:
        by_id = {ev.event_id: ev for ev in s.exec(select(UsageEvent)).all()}

        # Go usage stays on the Go card, untouched.
        assert by_id["msg_go"].provider_id == "opencode"
        assert by_id["msg_go"].kind == "message"
        assert by_id["msg_go"].tokens_input == 500

        # BYOK usage moves to its own dedicated card, usage numbers preserved.
        assert by_id["msg_byok"].provider_id == "opencode-byok"
        assert by_id["msg_byok"].kind == "message"
        assert by_id["msg_byok"].tokens_input == 999

        # The failed OpenRouter request moves to its sub-provider AND becomes
        # kind="error" with usage zeroed out — it never actually happened.
        err = by_id["msg_openrouter_err"]
        assert err.provider_id == "opencode-openrouter"
        assert err.kind == "error"
        assert err.stop_reason == "auth_failed"
        assert err.tokens_input == 0
        assert err.tokens_output == 0
        assert err.cost_usd == 0.0

        # Free tier was already correct — left alone.
        assert by_id["msg_free"].provider_id == "opencode-free"

        # Rollups rebuilt for every touched provider; the reclassified error
        # row must NOT contribute (rollups only cover kind="message").
        rollup_providers = {
            r.provider_id for r in s.exec(select(UsagePeriodRollup)).all() if r.msgs > 0
        }
        assert "opencode-byok" in rollup_providers
        assert "opencode" in rollup_providers
        assert "opencode-openrouter" not in rollup_providers


def test_migration_retags_minimax_coding_plan_and_forces_account_id(engine):
    """Events under 'opencode-minimax-coding-plan' (the fallback id OpenCode's
    minimax-coding-plan providerID mints) move to the canonical 'minimax'
    provider AND account_id 'default' — the identity the server's MiniMax
    quota collector uses — so they land on the same latest_usage card instead
    of a standalone opencode-minimax-coding-plan entry."""
    with Session(engine) as s:
        s.add(
            UsageEvent(
                provider_id="opencode-minimax-coding-plan",
                account_id="user@opencode.test",  # the OpenCode account email
                sidecar_id="local",
                event_id="msg_minimax",
                ts=NOW,
                kind="message",
                model_id="MiniMax-M3",
                tokens_input=90429,
                tokens_output=3102,
                tokens_cache_read=23355693,
                cost_usd=0.0,
            )
        )
        s.commit()

    db_path = _make_opencode_db(
        [
            {
                "id": "msg_minimax",
                "data": {
                    "role": "assistant",
                    "providerID": "minimax-coding-plan",
                    "modelID": "MiniMax-M3",
                    "cost": 0,
                    "tokens": {
                        "input": 90429,
                        "output": 3102,
                        "cache": {"read": 23355693, "write": 0},
                    },
                },
            }
        ]
    )

    try:
        with (
            patch("scripts.reclassify_opencode_providers.engine", engine),
            patch("scripts.backfill_rollups.engine", engine),
        ):
            from scripts.reclassify_opencode_providers import migrate

            changed = migrate(db_path, apply=True)
    finally:
        db_path.unlink(missing_ok=True)

    assert changed == 1
    with Session(engine) as s:
        ev = s.exec(select(UsageEvent)).one()
        assert ev.provider_id == "minimax"
        assert ev.account_id == "default"
        assert ev.kind == "message"
        # cost_usd untouched here — reclassify only retags identity;
        # scripts/recost_events.py handles repricing.
        assert ev.cost_usd == 0.0

        rollup_providers = {
            r.provider_id for r in s.exec(select(UsagePeriodRollup)).all() if r.msgs > 0
        }
        assert "minimax" in rollup_providers
        assert "opencode-minimax-coding-plan" not in rollup_providers


def test_migration_survives_and_restores_a_genuine_collision(engine):
    """Two usage_events rows sharing the same event_id under different old
    provider_ids (a pre-existing duplicate-ingestion artifact seen in real
    data, not something this migration creates) both resolve to the identical
    corrected target (same provider_id, account_id, event_id — _seed_event
    always uses account_id="default"). The first row to be processed
    succeeds; the second collides on the (provider_id, account_id, event_id)
    unique constraint.

    This must not crash the batch — session.begin_nested() autoflushes
    pending changes as part of establishing its SAVEPOINT, and if the row is
    mutated *before* that call, the autoflush's IntegrityError fires outside
    the try/except meant to catch it, killing the whole run on one collision.
    The loser row must also come back byte-identical to its pre-migration
    state, not partially mutated."""
    with Session(engine) as s:
        _seed_event(s, "msg_dup", "opencode-free", tokens_input=10, tokens_output=5)
        _seed_event(s, "msg_dup", "opencode", tokens_input=1, tokens_output=1)
        s.commit()

    db_path = _make_opencode_db(
        [
            {
                "id": "msg_dup",
                "data": {
                    "role": "assistant",
                    "providerID": "openrouter",
                    "modelID": "google/gemma-4-31b-it:free",
                    "cost": 0,
                    "tokens": {"input": 1, "output": 1},
                    "error": {"name": "APIError", "data": {"statusCode": 401}},
                },
            }
        ]
    )

    try:
        with (
            patch("scripts.reclassify_opencode_providers.engine", engine),
            patch("scripts.backfill_rollups.engine", engine),
        ):
            from scripts.reclassify_opencode_providers import migrate

            # Must return normally (not raise) despite the guaranteed collision.
            changed = migrate(db_path, apply=True)
    finally:
        db_path.unlink(missing_ok=True)

    # Exactly one row can win the (opencode-openrouter, default, msg_dup) slot.
    assert changed == 1

    with Session(engine) as s:
        rows = {r.provider_id: r for r in s.exec(select(UsageEvent)).all()}
        winner = rows["opencode-openrouter"]
        assert winner.kind == "error"
        assert winner.stop_reason == "auth_failed"
        assert winner.tokens_input == 0  # zeroed — this is the corrected error row

        # The loser is restored byte-identical to its pre-migration state —
        # NOT left half-mutated by the failed attempt.
        loser_provider_id = "opencode-free" if "opencode-free" in rows else "opencode"
        loser = rows[loser_provider_id]
        assert loser.kind == "message"
        original_tokens = 10 if loser_provider_id == "opencode-free" else 1
        assert loser.tokens_input == original_tokens


def test_migration_dry_run_writes_nothing(engine):
    with Session(engine) as s:
        _seed_event(s, "msg_byok", "opencode", tokens_input=999)
        s.commit()

    db_path = _make_opencode_db(
        [
            {
                "id": "msg_byok",
                "data": {
                    "role": "assistant",
                    "providerID": "open-design-byok",
                    "modelID": "tencent/hy3:free",
                    "cost": 0,
                    "tokens": {"input": 999},
                },
            }
        ]
    )

    try:
        with patch("scripts.reclassify_opencode_providers.engine", engine):
            from scripts.reclassify_opencode_providers import migrate

            changed = migrate(db_path, apply=False)
    finally:
        db_path.unlink(missing_ok=True)

    assert changed == 1
    with Session(engine) as s:
        ev = s.exec(select(UsageEvent)).one()
        assert ev.provider_id == "opencode"  # unchanged — dry run only
