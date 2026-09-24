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
    # Model a pre-migration DB: the scripts under test clean up
    # cross-account duplicate events that the (provider_id, event_id)
    # unique index now prevents on fresh databases.
    with eng.begin() as _conn:
        _conn.exec_driver_sql("DROP INDEX uq_usage_events_provider_event")
    with Session(eng) as s:
        seed_pricing_table(s)
        s.commit()
    return eng


def _ev(provider_id: str, account_id: str, event_id: str, **kw) -> UsageEvent:
    defaults = {
        "ts": NOW,
        "kind": "message",
        "model_id": "some-model",
        "tokens_input": 100,
        "tokens_output": 50,
        "cost_usd": 0.0,
    }
    for k, v in defaults.items():
        kw.setdefault(k, v)
    return UsageEvent(
        provider_id=provider_id,
        account_id=account_id,
        sidecar_id="local",
        event_id=event_id,
        **kw,
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

    assert counts.default_events_with_email_twin == 1
    assert counts.default_events_orphan_no_twin == 0

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

    assert counts.default_events_with_email_twin == 0
    assert counts.default_events_orphan_no_twin == 1

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

    assert counts.default_message_events_total == 1
    assert counts.default_events_with_email_twin == 1
    assert counts.default_events_orphan_no_twin == 0

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
        from scripts.merge_antigravity_accounts import _scan_twin_divergence

        diverge = _scan_twin_divergence(s, "user@example.com").per_field_counts

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

    # Round-5: dry-run is now advisory on the gauge gate too (only --apply
    # aborts). --dry-run prints the warning + continues; --apply aborts with
    # rc=3 unless --force is passed.
    with Session(engine) as s:
        argv_patch, engine_patch = patch_argv(s, ["--dry-run"])
        with argv_patch, engine_patch:
            result = merge_antigravity_accounts.main()
    assert result == 0  # dry-run completes; gauge gate is advisory

    # Now --apply without --force: must abort at rc=3.
    with Session(engine) as s, pytest.raises(SystemExit) as ei:
        argv_patch, engine_patch = patch_argv(s, ["--apply"])
        with argv_patch, engine_patch:
            merge_antigravity_accounts.main()
    assert ei.value.code == 3

    # No events were written (the gate aborted before DEDUP).
    with Session(engine) as s:
        events = s.exec(select(UsageEvent)).all()
    assert {(ev.account_id, ev.event_id) for ev in events} == {("user@example.com", "e_seed")}


def test_dry_run_does_not_abort_on_gauge_gate(engine, capsys):
    """Round-5 finding: `--dry-run` must remain runnable even with default-keyed
    gauge rows (same fix as the divergence gate — advisory in dry-run,
    abort on --apply unless --force).
    """
    from app.models.db import LatestUsage

    with Session(engine) as s:
        # Seed an email event so discovery succeeds and we reach the gauge gate.
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
        argv_patch, engine_patch = patch_argv(s, ["--dry-run"])
        with argv_patch, engine_patch:
            result = merge_antigravity_accounts.main()
    assert result == 0  # dry-run completes; gauge gate is advisory
    err = capsys.readouterr().err
    assert "GAUGE GATE WARNING" in err


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


def test_twin_divergence_gate_aborts_with_rc4_unless_force(engine):
    """Round-3 warning: a non-zero blocking divergence (tokens or cost_usd) is
    the one signal that the deleted `default` row may carry usage the
    survivor lacks. `--apply` must abort with rc=4 unless `--force`.
    """
    from app.models.db import LatestUsage

    with Session(engine) as s:
        # Email event with a different `tokens_input` from its default twin —
        # this is the exact shape the warning is about.
        s.add(_ev("antigravity", "user@example.com", "e_div", tokens_input=100, tokens_output=50))
        s.add(_ev("antigravity", "default", "e_div", tokens_input=200, tokens_output=50))
        # Empty `latest_usage` row so the gauge gate passes.
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="user@example.com",
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

    # Without --force, main() exits 4 on the divergence gate, before any writes.
    with Session(engine) as s:
        argv_patch, engine_patch = patch_argv(s, ["--apply"])
        with argv_patch, engine_patch, pytest.raises(SystemExit) as ei:
            merge_antigravity_accounts.main()
    assert ei.value.code == 4

    # Both events still exist — gate fired before DEDUP.
    with Session(engine) as s:
        events = s.exec(select(UsageEvent)).all()
    assert {(ev.account_id, ev.event_id) for ev in events} == {
        ("user@example.com", "e_div"),
        ("default", "e_div"),
    }

    # With --force, the same setup runs through to the destructive path.
    with Session(engine) as s:
        argv_patch, engine_patch = patch_argv(s, ["--apply", "--force"])
        with argv_patch, engine_patch:
            result = merge_antigravity_accounts.main()
    assert result == 0

    with Session(engine) as s:
        events = s.exec(select(UsageEvent)).all()
    assert {(ev.account_id, ev.event_id) for ev in events} == {("user@example.com", "e_div")}


def test_twin_divergence_gate_ignores_model_id_and_ts(engine):
    """Round-3 follow-up: `model_id` / `ts` divergences are the documented
    reclassify artifact (agy raw vs canonical name) — they must stay
    print-only and NOT trigger the rc=4 gate.
    """
    from app.models.db import LatestUsage

    with Session(engine) as s:
        # Same tokens and cost, different model_id (raw vs canonical).
        s.add(_ev("antigravity", "user@example.com", "e_model", model_id="gemini-2.5-flash"))
        s.add(_ev("antigravity", "default", "e_model", model_id="flash-3.8"))
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="user@example.com",
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

    # --apply with model_id-only divergence: gate does NOT trip, DEDUP runs.
    with Session(engine) as s:
        argv_patch, engine_patch = patch_argv(s, ["--apply"])
        with argv_patch, engine_patch:
            result = merge_antigravity_accounts.main()
    assert result == 0

    with Session(engine) as s:
        events = s.exec(select(UsageEvent)).all()
    assert {(ev.account_id, ev.event_id) for ev in events} == {("user@example.com", "e_model")}


def test_discovery_falls_back_to_latest_usage_when_no_message_events(engine):
    """Round-3 suggestion 2: a host whose canonical email exists only in
    `latest_usage`/`quota_snapshots` (the LSP-only shape) used to abort at
    rc=1. It now falls back to the gauge tables.
    """
    from app.models.db import LatestUsage

    with Session(engine) as s:
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="user@example.com",
                sidecar_id="local",
                window_type="weekly",
                variant="",
                model_id="",
                card_json="{}",
                updated_at=NOW,
            )
        )
        # Plus a default-account event so DEDUP has something to do.
        s.add(_ev("antigravity", "default", "e_only"))
        s.commit()

    from scripts import merge_antigravity_accounts

    with Session(engine) as s:
        email = merge_antigravity_accounts._discover_canonical_account(s)
    assert email == "user@example.com"


def test_main_apply_runs_all_phases_and_is_idempotent(engine):
    """Round-3 suggestion 3: end-to-end `--apply` test asserting the post-merge
    row set. The destructive path is the whole point — exercise it in CI.

    Covers `:389-421` (Phase D + summary) and `:430-431` (`--force` summary)
    that were uncovered before. A second `--apply` run is a no-op (the
    idempotence Hermes verified).
    """
    from app.models.db import LatestUsage, QuotaSnapshot

    with Session(engine) as s:
        # Three default message twins + one default error row (kind != 'message',
        # must survive DEDUP).
        for eid in ("e1", "e2", "e3"):
            s.add(_ev("antigravity", "default", eid))
            s.add(_ev("antigravity", "user@example.com", eid))
        s.add(_ev("antigravity", "default", "e_err", kind="error"))
        s.add(_ev("antigravity", "user@example.com", "e_err", kind="error"))
        # Plus a non-antigravity row that must be left untouched.
        s.add(_ev("chatgpt", "user@example.com", "chatgpt_1"))
        # Gauge: email-keyed latest_usage + quota_snapshots (must be preserved).
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="user@example.com",
                sidecar_id="local",
                window_type="weekly",
                variant="",
                model_id="",
                card_json="{}",
                updated_at=NOW,
            )
        )
        s.add(
            QuotaSnapshot(
                provider_id="antigravity",
                account_id="user@example.com",
                window_type="weekly",
                model_id="",
                ts=NOW,
                pct_used=50.0,
                reset_at=NOW,
                variant="",
            )
        )
        s.commit()

    from scripts import merge_antigravity_accounts

    # First --apply: destructive path runs.
    with Session(engine) as s:
        argv_patch, engine_patch = patch_argv(s, ["--apply"])
        with argv_patch, engine_patch:
            result = merge_antigravity_accounts.main()
    assert result == 0

    with Session(engine) as s:
        ag_events = s.exec(select(UsageEvent).where(UsageEvent.provider_id == "antigravity")).all()
    # Default message twins deleted; email rows + default error row preserved.
    assert {(ev.account_id, ev.event_id, ev.kind) for ev in ag_events} == {
        ("user@example.com", "e1", "message"),
        ("user@example.com", "e2", "message"),
        ("user@example.com", "e3", "message"),
        ("user@example.com", "e_err", "error"),
        ("default", "e_err", "error"),
    }

    # Gauge rows preserved.
    with Session(engine) as s:
        cards = s.exec(select(LatestUsage).where(LatestUsage.provider_id == "antigravity")).all()
        snaps = s.exec(
            select(QuotaSnapshot).where(QuotaSnapshot.provider_id == "antigravity")
        ).all()
    assert {c.account_id for c in cards} == {"user@example.com"}
    assert {sn.account_id for sn in snaps} == {"user@example.com"}

    # Non-antigravity rows untouched.
    with Session(engine) as s:
        chatgpt_events = s.exec(select(UsageEvent).where(UsageEvent.provider_id == "chatgpt")).all()
    assert {(ev.account_id, ev.event_id) for ev in chatgpt_events} == {
        ("user@example.com", "chatgpt_1")
    }

    # Second --apply: idempotent no-op.
    with Session(engine) as s:
        argv_patch, engine_patch = patch_argv(s, ["--apply"])
        with argv_patch, engine_patch:
            result = merge_antigravity_accounts.main()
    assert result == 0

    with Session(engine) as s:
        ag_events = s.exec(select(UsageEvent).where(UsageEvent.provider_id == "antigravity")).all()
    assert {(ev.account_id, ev.event_id, ev.kind) for ev in ag_events} == {
        ("user@example.com", "e1", "message"),
        ("user@example.com", "e2", "message"),
        ("user@example.com", "e3", "message"),
        ("user@example.com", "e_err", "error"),
        ("default", "e_err", "error"),
    }


def test_dry_run_phase_c_preview_matches_apply(engine, capsys):
    """Round-4 warning: the dry-run preview recompute (round-3 fix) ships with
    no test. Run `--dry-run` then `--apply` on one seeded DB; assert both
    print the same Phase C event count N (the operator's only pre-flight).
    """
    from app.models.db import LatestUsage

    with Session(engine) as s:
        for eid in ("e1", "e2", "e3"):
            s.add(_ev("antigravity", "default", eid))
            s.add(_ev("antigravity", "user@example.com", eid))
        s.add(_ev("antigravity", "default", "e_orphan"))  # default with no email twin
        s.add(
            _ev("antigravity", "user@example.com", "e_email_only")
        )  # email without a default twin
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="user@example.com",
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
        argv_patch, engine_patch = patch_argv(s, ["--dry-run"])
        with argv_patch, engine_patch:
            merge_antigravity_accounts.main()
    dry_run_out = capsys.readouterr().out

    # Phase C preview in dry-run: 5 surviving events (3 email twins + 1 orphan
    # left behind + 1 email-only). Pre-fix behavior would have printed 8.
    assert "Phase C done — rollups would rebuild from 5 event(s)." in dry_run_out

    # Reset stdout capture and run --apply on the same DB.
    capsys.readouterr()
    with Session(engine) as s:
        argv_patch, engine_patch = patch_argv(s, ["--apply"])
        with argv_patch, engine_patch:
            merge_antigravity_accounts.main()
    apply_out = capsys.readouterr().out

    assert "Phase C done — rollups rebuilt from 5 event(s)." in apply_out


def test_discovery_falls_back_to_quota_snapshots_when_only_quota_snapshots_present(engine):
    """Round-4 warning: a host whose email identity lives only in
    `quota_snapshots` (no `latest_usage` row, no `usage_events`) used to
    abort at rc=1. The fallback chain now includes `quota_snapshots`.
    """
    from app.models.db import QuotaSnapshot

    with Session(engine) as s:
        s.add(
            QuotaSnapshot(
                provider_id="antigravity",
                account_id="user@example.com",
                window_type="weekly",
                model_id="",
                ts=NOW,
                pct_used=50.0,
                reset_at=NOW,
                variant="",
            )
        )
        s.add(_ev("antigravity", "default", "e_only"))  # nothing to dedupe, just so we're not empty
        s.commit()

    from scripts import merge_antigravity_accounts

    with Session(engine) as s:
        email = merge_antigravity_accounts._discover_canonical_account(s)
    assert email == "user@example.com"


def test_divergence_gate_counts_distinct_event_ids(engine):
    """Round-4 suggestion: one twin pair differing on `tokens_input` AND
    `tokens_output` must print "1 twin pair(s)" — distinct event_ids, not
    per-field sums.
    """
    from app.models.db import LatestUsage

    with Session(engine) as s:
        # One pair: both tokens_input and tokens_output differ.
        s.add(_ev("antigravity", "default", "e_both", tokens_input=100, tokens_output=50))
        s.add(_ev("antigravity", "user@example.com", "e_both", tokens_input=200, tokens_output=99))
        # Second pair: only tokens_input differs.
        s.add(_ev("antigravity", "default", "e_one", tokens_input=100, tokens_output=50))
        s.add(_ev("antigravity", "user@example.com", "e_one", tokens_input=200, tokens_output=50))
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="user@example.com",
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
        divergent_ids = merge_antigravity_accounts._scan_twin_divergence(
            s, "user@example.com"
        ).divergent_event_ids
    assert sorted(divergent_ids) == ["e_both", "e_one"]
    assert len(divergent_ids) == 2  # two distinct event_ids, not four field sums


def test_divergence_gate_includes_cache_create_1h_5m(engine, capsys):
    """Round-4 suggestion: `tokens_cache_create_1h` / `_5m` are usage-bearing
    (2x / 1.25x base input multiplier). Divergences on these fields must
    trip the gate.
    """
    from app.models.db import LatestUsage

    with Session(engine) as s:
        # Same model_id and ts; tokens_cache_create_1h diverges.
        s.add(_ev("antigravity", "default", "e_1h", tokens_cache_create_1h=1000))
        s.add(_ev("antigravity", "user@example.com", "e_1h", tokens_cache_create_1h=0))
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="user@example.com",
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
        argv_patch, engine_patch = patch_argv(s, ["--apply"])
        with argv_patch, engine_patch, pytest.raises(SystemExit) as ei:
            merge_antigravity_accounts.main()
    assert ei.value.code == 4
    err = capsys.readouterr().err
    # The warning prints the offending event_id.
    assert "e_1h" in err


def test_divergence_gate_includes_cache_create_5m(engine):
    """Same as the 1h variant above, for `tokens_cache_create_5m`."""
    from app.models.db import LatestUsage

    with Session(engine) as s:
        s.add(_ev("antigravity", "default", "e_5m", tokens_cache_create_5m=1000))
        s.add(_ev("antigravity", "user@example.com", "e_5m", tokens_cache_create_5m=0))
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="user@example.com",
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
        divergent_ids = merge_antigravity_accounts._scan_twin_divergence(
            s, "user@example.com"
        ).divergent_event_ids
    assert divergent_ids == ["e_5m"]


def test_dry_run_does_not_abort_on_divergence_gate(engine, capsys):
    """Round-4 suggestion: `--dry-run` must remain runnable even with
    blocking divergence, so the operator can see the pre-flight. The
    apply path aborts with rc=4 (separate test).
    """
    from app.models.db import LatestUsage

    with Session(engine) as s:
        s.add(_ev("antigravity", "default", "e_div", tokens_input=100))
        s.add(_ev("antigravity", "user@example.com", "e_div", tokens_input=200))
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="user@example.com",
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

    # Dry-run + divergence: warning printed, exit 0, no writes.
    with Session(engine) as s:
        argv_patch, engine_patch = patch_argv(s, ["--dry-run"])
        with argv_patch, engine_patch:
            result = merge_antigravity_accounts.main()
    assert result == 0
    err = capsys.readouterr().err
    assert "TWIN DIVERGENCE WARNING" in err
    assert "e_div" in err

    # No events deleted in dry-run.
    with Session(engine) as s:
        events = s.exec(select(UsageEvent)).all()
    assert {(ev.account_id, ev.event_id) for ev in events} == {
        ("default", "e_div"),
        ("user@example.com", "e_div"),
    }


def test_apply_aborts_on_divergence_with_event_ids_in_message(engine, capsys):
    """Round-4 suggestion: `--apply` + divergence → rc=4, abort message
    includes the offending event_ids so the operator can act.
    """
    from app.models.db import LatestUsage

    with Session(engine) as s:
        s.add(_ev("antigravity", "default", "e_abort1", tokens_input=100))
        s.add(_ev("antigravity", "user@example.com", "e_abort1", tokens_input=200))
        s.add(_ev("antigravity", "default", "e_abort2", tokens_input=300))
        s.add(_ev("antigravity", "user@example.com", "e_abort2", tokens_input=400))
        s.add(
            LatestUsage(
                provider_id="antigravity",
                account_id="user@example.com",
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
        argv_patch, engine_patch = patch_argv(s, ["--apply"])
        with argv_patch, engine_patch, pytest.raises(SystemExit) as ei:
            merge_antigravity_accounts.main()
    assert ei.value.code == 4

    err = capsys.readouterr().err
    assert "TWIN DIVERGENCE WARNING" in err
    assert "e_abort1" in err
    assert "e_abort2" in err


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
