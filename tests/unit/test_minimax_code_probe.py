"""Unit tests for the MiniMax Code read-only probe."""

import json
from pathlib import Path

import pytest

from scripts.sidecar_pkg import minimax_code_probe as probe
from scripts.sidecar_pkg.event_extractors import minimax_code
from tests.fixtures.minimax_code_fixture import make_minimax_code_store


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host with a MiniMax Code runtime store and an isolated cursor dir."""
    make_minimax_code_store(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("MINIMAX_CODE_HOME", raising=False)
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


def test_the_probe_finds_the_store_in_the_windows_app_data_root(tmp_path, monkeypatch):
    """The desktop app's Windows root is probed, not just ~/.minimax."""
    make_minimax_code_store(tmp_path, data_dir=str(Path("AppData") / "Roaming" / "MiniMax Code"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("MINIMAX_CODE_HOME", raising=False)
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "config"))
    report = probe.probe_minimax_code()
    assert report["ok"] is True
    assert "MiniMax Code" in report["store"]
    assert "AppData" in report["store"]


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
