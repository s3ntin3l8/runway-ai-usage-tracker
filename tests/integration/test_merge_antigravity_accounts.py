"""Integration test for scripts/merge_antigravity_accounts.py.

Seeds `usage_events` with the dual-ingest pattern from production (every
default-account event has an email twin by `event_id`, plus a few edge
cases: a non-message default row, an orphan default row, a twin pair
that diverges on tokens) and asserts each phase of the merge produces the
expected state. Also covers the documented run-command regression —
`python scripts/merge_antigravity_accounts.py --help` from a non-repo
cwd must succeed (round-2 finding).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent
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


def test_dedup_deletes_default_twin_keeps_email(engine):
    """Round-1 invariant: DEDUP removes every default message-event with an email twin."""
    with Session(engine) as s:
        s.add(_ev("antigravity", "default", "e_dup"))
        s.add(_ev("antigravity", "user@example.com", "e_dup"))
        s.commit()

    with Session(engine) as s:
        from scripts.merge_antigravity_accounts import phase_dedup_delete

        counts = phase_dedup_delete(s, "user@example.com", dry_run=False)
        s.commit()

    assert counts["default_events_with_email_twin"] == 1
    assert counts["default_events_orphan_no_twin"] == 0

    with Session(engine) as s:
        remaining = s.exec(select(UsageEvent)).all()
    assert [ev.account_id for ev in remaining] == ["user@example.com"]
    assert [ev.event_id for ev in remaining] == ["e_dup"]


def test_dedup_leaves_orphan_default_alone(engine):
    """A default event with no email twin survives (left for an explicit follow-up)."""
    with Session(engine) as s:
        s.add(_ev("antigravity", "default", "e_orphan"))
        s.add(_ev("antigravity", "user@example.com", "e_other"))
        s.commit()

    with Session(engine) as s:
        from scripts.merge_antigravity_accounts import phase_dedup_delete

        counts = phase_dedup_delete(s, "user@example.com", dry_run=False)
        s.commit()

    assert counts["default_events_with_email_twin"] == 0
    assert counts["default_events_orphan_no_twin"] == 1

    with Session(engine) as s:
        remaining = s.exec(select(UsageEvent)).all()
    assert {(ev.account_id, ev.event_id) for ev in remaining} == {
        ("default", "e_orphan"),
        ("user@example.com", "e_other"),
    }


def test_dedup_skips_non_message_default_rows(engine):
    """DEDUP only touches kind='message' — error rows are surfaced, not deleted."""
    with Session(engine) as s:
        s.add(_ev("antigravity", "default", "e_err", kind="error"))
        s.add(_ev("antigravity", "user@example.com", "e_err", kind="error"))
        s.add(_ev("antigravity", "default", "e_msg"))
        s.add(_ev("antigravity", "user@example.com", "e_msg"))
        s.commit()

    with Session(engine) as s:
        from scripts.merge_antigravity_accounts import phase_dedup_delete

        counts = phase_dedup_delete(s, "user@example.com", dry_run=False)
        s.commit()

    assert counts["default_message_events_total"] == 1
    assert counts["default_events_with_email_twin"] == 1
    assert counts["default_events_orphan_no_twin"] == 0

    with Session(engine) as s:
        remaining = s.exec(select(UsageEvent)).all()
    by_pair = {(ev.account_id, ev.event_id, ev.kind) for ev in remaining}
    assert ("user@example.com", "e_msg", "message") in by_pair
    assert ("user@example.com", "e_err", "error") in by_pair
    assert ("default", "e_msg", "message") not in by_pair
    assert ("default", "e_err", "error") in by_pair  # both error twins survive


def test_discover_canonical_account_aborts_when_ambiguous(engine):
    """Two non-default accounts → SystemExit(2)."""
    with Session(engine) as s:
        s.add(_ev("antigravity", "user1@example.com", "e1"))
        s.add(_ev("antigravity", "user2@example.com", "e2"))
        s.commit()

    with Session(engine) as s:
        from scripts.merge_antigravity_accounts import _discover_canonical_account

        with pytest.raises(SystemExit) as ei:
            _discover_canonical_account(s)
    assert ei.value.code == 2


def test_twin_divergence_reports_field_disagreements(engine):
    """The pre-flight scan flags every twin that differs on tokens/cost/model/ts."""
    with Session(engine) as s:
        s.add(_ev("antigravity", "default", "e_match", tokens_input=100))
        s.add(_ev("antigravity", "user@example.com", "e_match", tokens_input=100))
        s.add(_ev("antigravity", "default", "e_diff", tokens_input=100, tokens_output=50))
        s.add(
            _ev(
                "antigravity",
                "user@example.com",
                "e_diff",
                tokens_input=100,
                tokens_output=99,
            )
        )
        s.commit()

    with Session(engine) as s:
        from scripts.merge_antigravity_accounts import _count_twin_divergence

        diverge = _count_twin_divergence(s, "user@example.com")

    assert diverge["tokens_output"] == 1
    assert diverge["any"] == 1


def test_gauge_gate_aborts_when_default_cards_present(engine):
    """phase_f_gauge_unchanged + the main() gate exit 3 unless --force."""
    from app.models.db import LatestUsage

    with Session(engine) as s:
        # Seed at least one email-account event so discovery succeeds and
        # the script reaches the gauge gate (not exit 1 at discovery).
        s.add(_ev("antigravity", "user@example.com", "e_seed"))
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="default",
                sidecar_id="local",
                window_type="weekly",
                variant="",
                model_id="",
                card_json="{}",
                updated_at=NOW,
            )
        )
        s.commit()

    from scripts import merge_antigravity_accounts

    with Session(engine) as s:
        gauge = merge_antigravity_accounts.phase_f_gauge_unchanged(s, "user@example.com")
    assert gauge["default_latest_usage"] == 1

    # Without --force, main() exits 3 before any writes.
    with Session(engine) as s, pytest.raises(SystemExit) as ei:
        argv_patch, engine_patch = patch_argv(s, ["--dry-run"])
        with argv_patch, engine_patch:
            merge_antigravity_accounts.main()
    assert ei.value.code == 3

    # No events were written.
    with Session(engine) as s:
        events = s.exec(select(UsageEvent)).all()
    assert {(ev.account_id, ev.event_id) for ev in events} == {("user@example.com", "e_seed")}


def test_documented_run_command_succeeds_from_non_repo_cwd():
    """Round-2 regression: `python scripts/merge_antigravity_accounts.py --help`
    must work from any CWD (the `_REPO_ROOT` sys.path insert must remain).
    """
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "merge_antigravity_accounts.py"

    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=tmp,
            env={**os.environ, "APP_HOST": "127.0.0.1"},
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, (
        f"documented command failed: rc={proc.returncode} stderr={proc.stderr[:500]}"
    )
    assert "usage: merge_antigravity_accounts.py" in proc.stdout


# --- helpers ---


def patch_argv(s: Session, extra_args: list[str]):
    """Patch sys.argv + the module-level `engine` for one main() call.

    The script uses a module-level `from app.core.db import engine`; we
    override it to point at our in-memory SQLite so the script's read-only
    probes (discover / count / divergence) hit our seeded rows.
    """
    from unittest.mock import patch

    return (
        patch.object(sys, "argv", ["merge_antigravity_accounts.py", *extra_args]),
        patch("scripts.merge_antigravity_accounts.engine", s.get_bind()),
    )
