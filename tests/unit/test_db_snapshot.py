"""Unit tests for pre-write database snapshotting in maintenance scripts."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import scripts.reprice_fallback_priced_events as reprice_script
from app.models.db import ProviderPricing, UsageEvent
from scripts._db_snapshot import prune_snapshots, take_snapshot

_DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def _init_file_db(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ProviderPricing(
                provider_id="anthropic",
                model_id="claude-sonnet-4-5",
                input_per_mtok=3.0,
                output_per_mtok=15.0,
                effective_from=datetime(2025, 1, 1, tzinfo=UTC).date(),
            )
        )
        session.add(
            UsageEvent(
                provider_id="anthropic",
                account_id="user@example.com",
                event_id="ev-1",
                ts=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
                kind="message",
                model_id="claude-sonnet-4-5",
                tokens_input=1_000_000,
                tokens_output=0,
                cost_usd=99.0,
                cost_estimated_usd=99.0,
                cost_source="estimated",
            )
        )
        session.commit()
    engine.dispose()


def test_snapshot_consistent_under_wal_with_uncheckpointed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = config_dir / "runway.db"
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(config_dir))

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items (name) VALUES ('initial')")
    conn.commit()

    # Second committed write that remains in -wal without checkpointing.
    conn.execute("INSERT INTO items (name) VALUES ('uncheckpointed')")
    conn.commit()

    wal_path = Path(f"{db_path}-wal")
    assert wal_path.exists() and wal_path.stat().st_size > 0

    try:
        snap_path = take_snapshot("test_script", db_path=db_path)
    finally:
        conn.close()

    assert snap_path is not None
    assert snap_path.parent == config_dir / "backups"
    assert snap_path.name.startswith("test_script-")
    assert snap_path.name.endswith(".db")
    if os.name != "nt":
        assert stat.S_IMODE(snap_path.parent.stat().st_mode) == 0o700

    out = capsys.readouterr().out
    assert str(snap_path) in out

    snap_conn = sqlite3.connect(str(snap_path))
    try:
        rows = snap_conn.execute("SELECT name FROM items ORDER BY id").fetchall()
    finally:
        snap_conn.close()
    assert rows == [("initial",), ("uncheckpointed",)]


def test_reprice_main_snapshot_dry_run_and_no_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = config_dir / "runway.db"
    _init_file_db(db_path)

    test_engine = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(reprice_script, "engine", test_engine)

    try:
        # 1. --dry-run leaves no snapshot and does not create backups/
        rc = reprice_script.main(
            ["--provider", "anthropic", "--model", "claude-sonnet-4-5", "--dry-run"]
        )
        assert rc == 0
        assert not (config_dir / "backups").exists()

        # 2. --no-snapshot leaves no snapshot and does not create backups/
        rc = reprice_script.main(
            ["--provider", "anthropic", "--model", "claude-sonnet-4-5", "--no-snapshot"]
        )
        assert rc == 0
        assert not (config_dir / "backups").exists()

        # 3. Normal run creates one snapshot and prints its path
        rc = reprice_script.main(["--provider", "anthropic", "--model", "claude-sonnet-4-5"])
        assert rc == 0
        backups = list((config_dir / "backups").iterdir())
        assert len(backups) == 1
        assert backups[0].name.startswith("reprice_fallback_priced_events-")
        out = capsys.readouterr().out
        assert str(backups[0]) in out
    finally:
        test_engine.dispose()


def test_retention_keeps_newest_two_and_preserves_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    backups_dir = config_dir / "backups"
    backups_dir.mkdir(parents=True)
    db_path = config_dir / "runway.db"
    _init_file_db(db_path)

    older_1 = backups_dir / "reprice_fallback_priced_events-20260101T000000Z.db"
    older_2 = backups_dir / "reprice_fallback_priced_events-20260201T000000Z.db"
    older_3 = backups_dir / "reprice_fallback_priced_events-20260301T000000Z.db"
    other_script = backups_dir / "recost_events-20260101T000000Z.db"
    unrelated = backups_dir / "notes.txt"
    for p in (older_1, older_2, older_3, other_script, unrelated):
        p.write_text("keep", encoding="utf-8")

    test_engine = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(reprice_script, "engine", test_engine)

    try:
        rc = reprice_script.main(["--provider", "anthropic", "--model", "claude-sonnet-4-5"])
        assert rc == 0
    finally:
        test_engine.dispose()

    remaining = sorted(p.name for p in backups_dir.iterdir())
    assert older_1.name not in remaining
    assert older_2.name not in remaining
    assert older_3.name in remaining
    assert other_script.name in remaining
    assert unrelated.name in remaining
    reprice_snaps = [n for n in remaining if n.startswith("reprice_fallback_priced_events-")]
    assert len(reprice_snaps) == 2


def test_prune_snapshots_never_deletes_protected_snapshot(tmp_path: Path) -> None:
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    older = backups_dir / "demo_script-20260101T000000Z.db"
    newest = backups_dir / "demo_script-20260201T000000Z.db"
    older.write_text("old", encoding="utf-8")
    newest.write_text("new", encoding="utf-8")

    deleted = prune_snapshots(backups_dir, "demo_script", keep=0, protect=newest)
    assert deleted == [older]
    assert newest.exists()
    assert not older.exists()


def test_disk_guard_aborts_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = config_dir / "runway.db"
    _init_file_db(db_path)
    before_bytes = db_path.read_bytes()

    test_engine = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(reprice_script, "engine", test_engine)

    # Fake free space strictly below 1.2x db_size.
    too_small = max(1, int(db_path.stat().st_size * 1.1))
    monkeypatch.setattr(
        "scripts._db_snapshot.shutil.disk_usage",
        lambda _path: _DiskUsage(total=10_000_000, used=10_000_000 - too_small, free=too_small),
    )

    try:
        with pytest.raises(SystemExit) as exc_info:
            reprice_script.main(["--provider", "anthropic", "--model", "claude-sonnet-4-5"])
        assert exc_info.value.code != 0
    finally:
        test_engine.dispose()

    err = capsys.readouterr().err
    assert "insufficient disk space" in err
    assert not (config_dir / "backups").exists()
    assert db_path.read_bytes() == before_bytes

    # Confirm the DB row was not mutated (cost_usd is still 99.0).
    check_engine = create_engine(f"sqlite:///{db_path}")
    try:
        with Session(check_engine) as session:
            ev = session.exec(select(UsageEvent)).one()
            assert ev.cost_usd == 99.0
    finally:
        check_engine.dispose()


_WRITING_SCRIPTS = [
    "assign_default_events.py",
    "backfill_argon_models.py",
    "backfill_cache_costs.py",
    "backfill_claude_billing_type.py",
    "backfill_claude_dimensions.py",
    "backfill_opencode_effort.py",
    "backfill_project_context.py",
    "backfill_rollups.py",
    "cleanup_anthropic_sonnet_weekly.py",
    "cleanup_antigravity.py",
    "cleanup_chatgpt_stale_window.py",
    "cleanup_minimax_monthly_cards.py",
    "cleanup_opencode_free_card.py",
    "cleanup_opencode_kimi_card.py",
    "collapse_default_account_events.py",
    "consolidate_session_projects.py",
    "dedup_usage_windows.py",
    "label_cloud_session_lead_repo.py",
    "link_minimax_background_sessions.py",
    "merge_antigravity_accounts.py",
    "merge_gemini_default_account.py",
    "migrate_kimi_coding_key.py",
    "normalize_sidecar_ids.py",
    "reclassify_antigravity_models.py",
    "reclassify_chatgpt_models.py",
    "reclassify_hermes_pending.py",
    "reclassify_opencode_providers.py",
    "recost_events.py",
    "remove_cloud_coarse_events.py",
    "reprice_fallback_priced_events.py",
    "reset_cloud_events.py",
]


@pytest.mark.parametrize("script_name", _WRITING_SCRIPTS)
def test_every_writing_script_wires_snapshot_helpers(script_name: str) -> None:
    src = (Path(__file__).resolve().parents[2] / "scripts" / script_name).read_text(
        encoding="utf-8"
    )
    assert "add_snapshot_args" in src
    assert "take_snapshot" in src
    assert "prune_snapshots" in src
