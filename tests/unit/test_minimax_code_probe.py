"""Unit tests for the MiniMax Code read-only probe."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.sidecar_pkg import minimax_code_probe as probe
from scripts.sidecar_pkg.event_extractors import minimax_code
from tests.fixtures.minimax_code_fixture import DEFAULT_USAGE, make_minimax_code_store


@pytest.fixture(autouse=True)
def _clean_data_env(monkeypatch):
    """Clear every env var that can move the runtime's data dir.

    Without this a developer's own ``MINIMAX_DATA_DIR`` (or the runtime's
    internal overrides) would leak into the probe's discovery.
    ``APPDATA`` goes with them: the Windows app-data root is read from it,
    so on a Windows host the developer's real roaming dir would otherwise
    be probed.
    """
    for name in ("MINIMAX_CODE_HOME", "APPDATA", *minimax_code._DATA_DIR_ENV_VARS):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host with a MiniMax Code runtime store and an isolated cursor dir."""
    make_minimax_code_store(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path


def test_probe_requirement_matches_the_extractor():
    """The probe's documented requirement is the extractor's requirement."""
    assert probe.REQUIRED_COLUMNS == minimax_code._REQUIRED_COLUMNS
    assert probe._MAX_ROWS_PER_CYCLE == minimax_code._MAX_ROWS_PER_CYCLE


def test_probe_reports_the_store_and_passes(host):
    report = probe.probe_minimax_code()
    assert report["ok"] is True
    assert report["store"] == str(host / ".minimax" / "v2" / "sqlite" / "runtime-state.sqlite")
    assert report["schema_ok"] is True
    assert report["missing_tables"] == []
    assert report["missing_columns"] == {}
    assert report["requests"] > 0
    assert report["turns"] > 0
    assert report["sessions"] > 0
    assert set(report["models"]) == {"MiniMax-M3.1-Flash", "(unresolved)"}
    # The fixture's mvs_other session names no model anywhere — the
    # telemetry, the history JSONL and llm-call.json are all absent — so
    # exactly one request comes back without a model.
    assert report["models"]["(unresolved)"] == 1
    assert report["plan_review_requests"] > 0
    # The fixture's rows are inside the first-run window.
    assert report["out_of_window_requests"] == 0
    assert report["skipped_requests"] == 0
    assert report["abandoned_requests"] == 0
    assert report["warnings"] == []


def test_probe_reports_the_rows_outside_the_first_run_window(tmp_path, monkeypatch):
    """The rows a first run would not import have to be visible.

    While the cursor advances every row is reported, but a rebuilt store or
    a deleted cursor file starts from the window — so the operator has to
    know how much of the ledger sits outside it.
    """
    old_ts = 1_600_000_000_000  # 2020-09-13, far outside any window
    usage = [{**DEFAULT_USAGE[0], "id": 1, "ts": old_ts}]
    make_minimax_code_store(tmp_path, usage=usage)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "config"))

    report = probe.probe_minimax_code()

    assert report["ok"] is True
    assert report["out_of_window_requests"] == 1
    assert any("SIDECAR_BOOTSTRAP_DAYS" in warning for warning in report["warnings"])
    text = probe.format_report(report)
    assert "not imported" in text
    assert "WARNING" in text


def test_probe_counts_only_the_rows_a_first_run_really_skips(tmp_path, monkeypatch):
    """The window's floor is on the id axis, not on the timestamp.

    A first run starts at the lowest id whose timestamp falls inside the
    window, so a row with an older timestamp but a higher id is still
    imported. Counting every old-timestamped row would overstate the gap.
    """
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    usage = [
        {**DEFAULT_USAGE[0], "id": 1, "ts": now_ms - 200 * 86_400_000},
        {**DEFAULT_USAGE[1], "id": 2, "ts": now_ms},
        {**DEFAULT_USAGE[2], "id": 3, "ts": now_ms - 200 * 86_400_000},
    ]
    make_minimax_code_store(tmp_path, usage=usage)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "config"))

    report = probe.probe_minimax_code()

    # The first run starts at id 2, so only the row before it is skipped.
    assert report["out_of_window_requests"] == 1
    assert report["requests"] == 3


def test_probe_reports_the_skips_its_own_extraction_finds(tmp_path, monkeypatch):
    """A skip the probe's own run finds has to be reported.

    The probe runs the real extractor and never commits, so the numbers
    exist only in the extractor's parked bookkeeping — the cursor file on
    disk cannot say them.
    """
    usage = [
        {**DEFAULT_USAGE[0], "id": 1},
        {**DEFAULT_USAGE[1], "id": 2, "ts": "not-a-timestamp"},
    ]
    make_minimax_code_store(tmp_path, usage=usage)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "config"))

    report = probe.probe_minimax_code()

    assert report["skipped_requests"] == 1
    assert any("skipped" in warning for warning in report["warnings"])
    text = probe.format_report(report)
    assert "skipped requests    : 1" in text
    assert "WARNING" in text


def test_probe_reports_the_skipped_and_abandoned_rows(host):
    """A row the extractor gave up on has to reach the operator."""
    cursor = probe._cursor_path()
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(
        json.dumps(
            {
                "databases": {
                    "/db/runtime-state.sqlite": {
                        "last_id": 42,
                        "skipped": {"41": 1},
                        "abandoned": [7],
                    }
                }
            }
        )
    )

    report = probe.probe_minimax_code()

    assert report["skipped_requests"] == 1
    assert report["abandoned_requests"] == 1
    assert any("abandoned" in warning for warning in report["warnings"])
    text = probe.format_report(report)
    assert "skipped requests" in text
    assert "abandoned requests" in text


def test_probe_fails_when_the_runtime_never_ran(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("MINIMAX_CODE_HOME", raising=False)
    report = probe.probe_minimax_code()
    assert report["ok"] is False
    assert report["store"] is None
    assert "no MiniMax Code runtime store" in report["problem"]


def test_probe_reports_schema_drift(host):
    import sqlite3

    conn = sqlite3.connect(host / ".minimax" / "v2" / "sqlite" / "runtime-state.sqlite")
    conn.execute("ALTER TABLE local_runtime_token_usage DROP COLUMN cache_read_tokens")
    conn.commit()
    conn.close()
    report = probe.probe_minimax_code()
    assert report["ok"] is False
    assert report["schema_ok"] is False
    assert "cache_read_tokens" in report["missing_columns"]["local_runtime_token_usage"]
    assert "drifted" in report["problem"]


def test_probe_reports_a_missing_table(host):
    import sqlite3

    conn = sqlite3.connect(host / ".minimax" / "v2" / "sqlite" / "runtime-state.sqlite")
    conn.execute("DROP TABLE local_runtime_message_rows")
    conn.commit()
    conn.close()
    report = probe.probe_minimax_code()
    assert report["ok"] is False
    assert "local_runtime_message_rows" in report["missing_tables"]


def test_probe_never_writes_the_cursor(host):
    """A probe run must not move the cursor: it only reads."""
    cursor = host / "config" / "sidecar" / "minimax_code_watermark.json"
    assert not cursor.exists()
    probe.probe_minimax_code()
    assert not cursor.exists()


def test_probe_leaves_an_existing_cursor_untouched(host):
    cursor = host / "config" / "sidecar" / "minimax_code_watermark.json"
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(json.dumps({"databases": {"/db": {"last_id": 7}}}), encoding="utf-8")
    before = cursor.read_bytes()
    report = probe.probe_minimax_code()
    assert cursor.read_bytes() == before
    assert report["cursor_last_id"] == 7


def test_probe_command_exits_zero_when_the_store_is_readable(host, capsys):
    with pytest.raises(SystemExit) as excinfo:
        probe.probe_command(["--probe-minimax-code"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out
    assert "minimax_code" in out


def test_probe_command_exits_one_when_there_is_nothing_to_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("MINIMAX_CODE_HOME", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        probe.probe_command(["--probe-minimax-code"])
    assert excinfo.value.code == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


def test_probe_command_ignores_other_flags():
    assert probe.probe_command([]) is False
    assert probe.probe_command(["--daemon"]) is False


def test_probe_command_prints_json(host, capsys):
    with pytest.raises(SystemExit):
        probe.probe_command(["--probe-minimax-code", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["schema_ok"] is True
    assert payload["platform"]


def test_probe_finds_the_store_under_a_windows_style_root(tmp_path, monkeypatch):
    """The same relative layout is found on a Windows-style home."""
    make_minimax_code_store(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("MINIMAX_CODE_HOME", raising=False)
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "config"))
    report = probe.probe_minimax_code()
    assert report["ok"] is True
    assert report["store"].endswith(
        str(Path(".minimax") / "v2" / "sqlite" / "runtime-state.sqlite")
    )


def test_the_cursor_path_follows_the_platform(tmp_path, monkeypatch):
    """On Windows the cursor sits under %APPDATA%, next to the sidecar's state."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.delenv("RUNWAY_CONFIG_DIR", raising=False)
    monkeypatch.setattr(probe.platform, "system", lambda: "Windows")
    assert probe._cursor_path() == (
        tmp_path / "AppData" / "Roaming" / "runway" / "sidecar" / "minimax_code_watermark.json"
    )


def test_the_cursor_path_follows_the_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "cfg"))
    assert probe._cursor_path() == tmp_path / "cfg" / "sidecar" / "minimax_code_watermark.json"


def test_the_probe_uses_the_sidecar_bootstrap_window(monkeypatch):
    """The dry run looks back as far as the sidecar's first run does."""
    from scripts.sidecar_pkg.bootstrap import DEFAULT_BOOTSTRAP_DAYS

    monkeypatch.delenv("SIDECAR_BOOTSTRAP_DAYS", raising=False)
    assert probe.bootstrap_window_days() == DEFAULT_BOOTSTRAP_DAYS


def test_the_probe_follows_the_bootstrap_days_override(monkeypatch):
    """The sidecar reads SIDECAR_BOOTSTRAP_DAYS; the probe must agree."""
    monkeypatch.setenv("SIDECAR_BOOTSTRAP_DAYS", "7")
    assert probe.bootstrap_window_days() == 7
    monkeypatch.setenv("SIDECAR_BOOTSTRAP_DAYS", "not-a-number")
    assert probe.bootstrap_window_days() == 90


def test_the_sidecar_and_the_probe_share_the_bootstrap_window(monkeypatch):
    """Neither may parse the env var on its own.

    The probe used to re-state the sidecar's expression, which meant a
    change to the sidecar's default would have left the probe reading a
    different window. Both now call one helper, and this pins the sidecar
    to it — on the parsed tree, so a comment that merely names the variable
    cannot trip it.
    """
    import ast

    import scripts.sidecar as sidecar_module
    from scripts.sidecar_pkg.bootstrap import bootstrap_window_days

    monkeypatch.setenv("SIDECAR_BOOTSTRAP_DAYS", "3")
    assert probe.bootstrap_window_days() == bootstrap_window_days() == 3

    tree = ast.parse(Path(sidecar_module.__file__).read_text(encoding="utf-8"))
    collector = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_collection"
    )
    called = {
        node.func.id
        for node in ast.walk(collector)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "bootstrap_window_days" in called
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getenv"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "SIDECAR_BOOTSTRAP_DAYS"
        for node in ast.walk(collector)
    )


@pytest.fixture
def _windows_host(monkeypatch):
    """Make the extractor believe it runs on Windows.

    The app-data root is read from ``%APPDATA%`` only on Windows, the same
    way the sidecar's own state dir reads it, so the tests that exercise
    that branch have to say which platform they mean.
    """
    monkeypatch.setattr(minimax_code.platform, "system", lambda: "Windows")


def test_the_probe_finds_the_store_in_the_windows_app_data_root(
    tmp_path, monkeypatch, _windows_host
):
    """The desktop app's Windows root is probed, not just ~/.minimax."""
    make_minimax_code_store(tmp_path, data_dir=str(Path("AppData") / "Roaming" / "MiniMax Code"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "config"))
    # The Windows app-data root is read from %APPDATA%; point it at the
    # same place so the test means the same thing on every platform.
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    report = probe.probe_minimax_code()
    assert report["ok"] is True
    assert "MiniMax Code" in report["store"]
    assert "AppData" in report["store"]


def test_the_probe_follows_a_redirected_windows_app_data_root(tmp_path, monkeypatch, _windows_host):
    """A host that relocates ``%APPDATA%`` keeps its app data there."""
    roaming = tmp_path / "Roaming"
    make_minimax_code_store(tmp_path, data_dir=str(Path("Roaming") / "MiniMax Agent"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "elsewhere"))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(roaming))
    report = probe.probe_minimax_code()
    assert report["ok"] is True
    assert str(roaming) in report["store"]


def test_the_probe_reports_every_store_on_the_host(host, monkeypatch):
    """A profile-scoped install keeps a second ledger; both are reported."""
    make_minimax_code_store(host, data_dir=".minimax-work")
    report = probe.probe_minimax_code()
    assert report["ok"] is True
    assert len(report["stores"]) == 2
    assert {detail["path"] for detail in report["stores"]} == {
        str(host / ".minimax" / "v2" / "sqlite" / "runtime-state.sqlite"),
        str(host / ".minimax-work" / "v2" / "sqlite" / "runtime-state.sqlite"),
    }
    # The counts are the whole host's: the sidecar reports every store.
    assert report["requests"] == sum(detail["requests"] for detail in report["stores"])
    assert report["pending_requests"] == sum(
        detail["pending_requests"] for detail in report["stores"]
    )


def test_the_probe_names_the_other_stores_in_its_report(host, monkeypatch):
    """The text report has to say which store is which."""
    make_minimax_code_store(host, data_dir=".minimax-work")
    text = probe.format_report(probe.probe_minimax_code())
    assert "stores found        : 2" in text
    assert "other stores:" in text
    assert ".minimax-work" in text


def test_the_probe_passes_when_one_store_drifted_and_another_did_not(host, monkeypatch):
    """A drifted store must not hide a readable one."""
    import sqlite3

    make_minimax_code_store(host, data_dir=".minimax-work")
    conn = sqlite3.connect(host / ".minimax-work" / "v2" / "sqlite" / "runtime-state.sqlite")
    conn.execute("ALTER TABLE local_runtime_token_usage DROP COLUMN cache_read_tokens")
    conn.commit()
    conn.close()

    report = probe.probe_minimax_code()
    assert report["ok"] is True
    assert report["schema_ok"] is True
    drifted = [d for d in report["stores"] if not d["schema_ok"]]
    assert len(drifted) == 1
    assert "cache_read_tokens" in drifted[0]["missing_columns"]["local_runtime_token_usage"]


def test_the_probe_reports_nothing_when_the_runtime_never_ran(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    report = probe.probe_minimax_code()
    assert report["ok"] is False
    assert report["stores"] == []


def test_the_probe_reports_an_unreadable_store_with_its_error(host, monkeypatch):
    """A store that cannot be opened is an error, not a schema drift."""
    real_introspect = probe._introspect

    def fake_introspect(db_path):
        if ".minimax" in str(db_path):
            return {
                "error": "cannot open the store read-only: disk I/O error",
                "tables": {},
                "missing_tables": [],
                "missing_columns": {},
            }
        return real_introspect(db_path)

    monkeypatch.setattr(probe, "_introspect", fake_introspect)
    report = probe.probe_minimax_code()
    assert report["ok"] is False
    assert "disk I/O error" in report["problem"]
    assert "drifted" not in report["problem"]


def test_an_unreadable_store_does_not_hide_a_readable_one(host, monkeypatch):
    """One broken store must not cost the host its working one."""
    make_minimax_code_store(host, data_dir=".minimax-work")
    real_introspect = probe._introspect

    def fake_introspect(db_path):
        if ".minimax-work" in str(db_path):
            return {
                "error": "cannot open the store read-only: disk I/O error",
                "tables": {},
                "missing_tables": [],
                "missing_columns": {},
            }
        return real_introspect(db_path)

    monkeypatch.setattr(probe, "_introspect", fake_introspect)
    report = probe.probe_minimax_code()
    assert report["ok"] is True
    assert report["store"].endswith(
        str(Path(".minimax") / "v2" / "sqlite" / "runtime-state.sqlite")
    )
    broken = [d for d in report["stores"] if d["error"]]
    assert len(broken) == 1


def test_a_store_whose_parse_raises_does_not_abort_the_report(host, monkeypatch):
    """The parse is failure-isolated per store, like the other two steps."""
    make_minimax_code_store(host, data_dir=".minimax-work")
    real_parse = minimax_code.parse_minimax_code_events

    def fake_parse(db_path, **kwargs):
        if ".minimax-work" in str(db_path):
            raise RuntimeError("the store exploded")
        return real_parse(db_path, **kwargs)

    monkeypatch.setattr(minimax_code, "parse_minimax_code_events", fake_parse)
    report = probe.probe_minimax_code()
    assert report["ok"] is True
    assert report["store"].endswith(
        str(Path(".minimax") / "v2" / "sqlite" / "runtime-state.sqlite")
    )
    broken = [d for d in report["stores"] if d["error"]]
    assert len(broken) == 1
    assert "exploded" in broken[0]["error"]


def test_the_report_tells_unreadable_from_incompatible(host, monkeypatch):
    """A store that cannot be read is not the same as one that drifted."""
    make_minimax_code_store(host, data_dir=".minimax-work")
    real_parse = minimax_code.parse_minimax_code_events

    def fake_parse(db_path, **kwargs):
        if ".minimax-work" in str(db_path):
            raise RuntimeError("the store exploded")
        return real_parse(db_path, **kwargs)

    monkeypatch.setattr(minimax_code, "parse_minimax_code_events", fake_parse)
    text = probe.format_report(probe.probe_minimax_code())
    assert "UNREADABLE" in text
    assert "INCOMPATIBLE" not in text


def test_a_drifted_store_is_reported_as_incompatible(host, monkeypatch):
    import sqlite3

    make_minimax_code_store(host, data_dir=".minimax-work")
    conn = sqlite3.connect(host / ".minimax-work" / "v2" / "sqlite" / "runtime-state.sqlite")
    conn.execute("ALTER TABLE local_runtime_token_usage DROP COLUMN cache_read_tokens")
    conn.commit()
    conn.close()

    text = probe.format_report(probe.probe_minimax_code())
    assert "INCOMPATIBLE" in text
    assert "UNREADABLE" not in text


def test_the_row_cap_is_stated_per_store(host):
    """The cap is per store, so the number must not read as the cycle's total."""
    text = probe.format_report(probe.probe_minimax_code())
    assert "rows per store" in text


def test_a_windowed_build_without_a_console_writes_the_report_to_a_file(host, monkeypatch, capsys):
    """A console=False build has sys.stdout is None; the probe must not crash."""
    report = probe.format_report(probe.probe_minimax_code())
    monkeypatch.setattr(probe.sys, "stdout", None)
    monkeypatch.setattr(probe, "_attach_parent_console", lambda: None)
    monkeypatch.setattr(probe, "_notify_windows", lambda message: None)
    probe._emit(report)
    written = host / "config" / "sidecar" / "minimax_code_probe.txt"
    assert written.exists()
    assert "RESULT: PASS" in written.read_text(encoding="utf-8")


def test_a_report_that_cannot_be_written_says_so(host, monkeypatch):
    """A failed write must not promise a report file that does not exist."""
    messages: list[str] = []
    monkeypatch.setattr(probe.sys, "stdout", None)
    monkeypatch.setattr(probe, "_attach_parent_console", lambda: None)
    monkeypatch.setattr(probe, "_notify_windows", messages.append)
    monkeypatch.setattr(probe, "_write_report_file", lambda text: None)
    probe._emit("RESULT: PASS")
    assert messages and "could not write" in messages[0]


def test_the_console_code_page_is_restored_after_printing(monkeypatch):
    """The output code page is process-wide: the probe must put it back."""
    import io

    buffer = io.StringIO()
    restored: list[int | None] = []

    def fake_attach() -> int:
        probe.sys.stdout = buffer
        return 437

    monkeypatch.setattr(probe.sys, "stdout", None)
    monkeypatch.setattr(probe, "_attach_parent_console", fake_attach)
    monkeypatch.setattr(probe, "_restore_console_code_page", restored.append)
    probe._emit("RESULT: PASS")
    assert restored == [437]
    assert "RESULT: PASS" in buffer.getvalue()


def test_a_console_that_cannot_be_opened_restores_the_code_page(monkeypatch):
    """A failure after the code page switched must not leak it.

    The attach helper returns None on failure, so `_emit` has nothing to
    restore — the helper itself has to put the console back before it
    gives up.
    """
    import sys
    import types

    calls: list[int | None] = []

    class _Kernel32:
        @staticmethod
        def AttachConsole(_pid: int) -> int:
            return 1

        @staticmethod
        def GetConsoleOutputCP() -> int:
            return 437

        @staticmethod
        def SetConsoleOutputCP(_cp: int) -> int:
            return 1

    fake_ctypes = types.ModuleType("ctypes")
    fake_ctypes.windll = types.SimpleNamespace(kernel32=_Kernel32())
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setattr(probe, "_restore_console_code_page", calls.append)
    monkeypatch.setattr(probe.sys, "platform", "win32")
    monkeypatch.setattr(probe.sys, "stdout", None)

    real_open = open

    def fake_open(name, *args, **kwargs):
        if name == "CONOUT$":
            raise OSError("no console")
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    assert probe._attach_parent_console() is None
    assert calls == [437]
