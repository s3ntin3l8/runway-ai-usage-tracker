"""Unit tests for pre-write database snapshotting in maintenance scripts."""

from __future__ import annotations

import ast
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


def test_snapshot_mkdir_failure_exits_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = config_dir / "runway.db"
    _init_file_db(db_path)

    # Occupy backups/ with a regular file so mkdir() fails.
    (config_dir / "backups").write_text("not a directory", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        take_snapshot("test_script", db_path=db_path, config_dir=config_dir)
    assert exc_info.value.code != 0

    err = capsys.readouterr().err
    assert "Error: failed to snapshot database" in err


_NON_MAINTENANCE_DB_SCRIPTS = {
    "_db_snapshot.py",
    "benchmark_server_memory.py",  # writes to a scratch temp DB
    "check_bridge_sessions.py",  # read-only diagnostic
}


def _discover_writing_scripts() -> list[Path]:
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    discovered: list[Path] = []
    for path in sorted(scripts_dir.glob("*.py")):
        if path.name in _NON_MAINTENANCE_DB_SCRIPTS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports_db = any(
            isinstance(node, ast.ImportFrom) and node.module == "app.core.db"
            for node in ast.walk(tree)
        )
        if imports_db:
            discovered.append(path)
    return discovered


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _is_db_write_call(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Name) and (
        node.func.id == "init_db"
        or node.func.id.startswith("apply_")
        or node.func.id == "merge_gauge_series"
    ):
        return True
    if isinstance(node.func, ast.Attribute):
        if node.func.attr in {"commit", "add", "delete", "execute"}:
            return True
        if node.func.attr == "exec" and node.args:
            return any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id in {"update", "delete", "insert"}
                for sub in ast.walk(node.args[0])
            )
    return False


def _contains_db_write_call(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Call) and _is_db_write_call(child) for child in ast.walk(node))


def _if_guards_snap(test: ast.expr) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "snap" for n in ast.walk(test))


@pytest.mark.parametrize("script_path", _discover_writing_scripts(), ids=lambda p: p.name)
def test_every_writing_script_wires_snapshot_helpers(script_path: Path) -> None:
    tree = ast.parse(script_path.read_text(encoding="utf-8"), filename=str(script_path))
    top_funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "main" in top_funcs, f"{script_path.name} missing main()"
    main_fn = top_funcs["main"]

    writer_helpers = {
        name for name, fn in top_funcs.items() if name != "main" and _contains_db_write_call(fn)
    }
    # Propagate transitively through local helper calls (e.g. main -> run -> phase_b -> apply_*).
    changed = True
    while changed:
        changed = False
        for name, fn in top_funcs.items():
            if name == "main" or name in writer_helpers:
                continue
            if any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in writer_helpers
                for n in ast.walk(fn)
            ):
                writer_helpers.add(name)
                changed = True

    add_args_lines: list[int] = []
    take_snap_lines: list[int] = []
    write_lines: list[int] = []

    for node in ast.walk(main_fn):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "add_snapshot_args":
                add_args_lines.append(node.lineno)
            elif name == "take_snapshot":
                take_snap_lines.append(node.lineno)
            elif _is_db_write_call(node) or (
                isinstance(node.func, ast.Name) and node.func.id in writer_helpers
            ):
                write_lines.append(node.lineno)

    assert add_args_lines, f"{script_path.name}: main() must call add_snapshot_args()"
    assert take_snap_lines, f"{script_path.name}: main() must call take_snapshot()"
    assert write_lines, f"{script_path.name}: expected DB write/init_db call in main()"
    assert min(take_snap_lines) < min(write_lines), (
        f"{script_path.name}: take_snapshot() (line {min(take_snap_lines)}) must precede "
        f"first DB write / init_db (line {min(write_lines)})"
    )

    # Every prune_snapshots() call in main() must be guarded by an `if` referencing `snap`.
    prune_calls = [
        node
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Call) and _call_name(node) == "prune_snapshots"
    ]
    assert prune_calls, f"{script_path.name}: main() must call prune_snapshots()"

    guarded_prunes: set[int] = set()
    for node in ast.walk(main_fn):
        if isinstance(node, ast.If) and _if_guards_snap(node.test):
            for body_node in node.body:
                for sub in ast.walk(body_node):
                    if isinstance(sub, ast.Call) and _call_name(sub) == "prune_snapshots":
                        guarded_prunes.add(id(sub))

    for call in prune_calls:
        assert id(call) in guarded_prunes, (
            f"{script_path.name}: prune_snapshots() at line {call.lineno} must be guarded "
            f"on `snap is not None`"
        )
