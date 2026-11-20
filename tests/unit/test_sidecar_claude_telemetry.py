"""Unit tests for Claude Code ~/.claude/settings.json telemetry management (#1366)."""

import json
import logging
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.sidecar_pkg import claude_telemetry as ct


def test_enable_when_file_missing_creates_file_and_disable_cleans_up(tmp_path: Path):
    settings_path = tmp_path / ".claude" / "settings.json"
    state_path = tmp_path / "state" / "claude-telemetry-state.json"

    status = ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)
    assert status == "enabled"
    assert settings_path.exists()
    assert not ct.backup_path_for(settings_path).exists()

    parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    assert parsed == {"env": dict(ct.TELEMETRY_ENV_KEYS)}

    status = ct.sync_claude_telemetry(False, settings_path=settings_path, state_path=state_path)
    assert status == "disabled"
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {}
    assert not state_path.exists()


def test_enable_when_file_has_no_env_preserves_other_keys_and_indentation(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "state.json"
    original_obj = {
        "model": "sonnet",
        "permissions": {"allow": ["Bash(git status)"]},
    }
    original_text = json.dumps(original_obj, indent=4) + "\n"
    settings_path.write_text(original_text, encoding="utf-8")

    status = ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)
    assert status == "enabled"

    updated_text = settings_path.read_text(encoding="utf-8")
    assert '\n    "model":' in updated_text
    assert '\n    "env":' in updated_text
    assert ct.backup_path_for(settings_path).read_text(encoding="utf-8") == original_text

    parsed = json.loads(updated_text)
    assert parsed["model"] == "sonnet"
    assert parsed["permissions"] == {"allow": ["Bash(git status)"]}
    assert parsed["env"] == ct.TELEMETRY_ENV_KEYS

    assert (
        ct.sync_claude_telemetry(False, settings_path=settings_path, state_path=state_path)
        == "disabled"
    )
    assert json.loads(settings_path.read_text(encoding="utf-8")) == original_obj


def test_enable_preserves_existing_env_keys_and_disable_restores_exact_json(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "state.json"
    original_obj = {
        "theme": "dark",
        "env": {
            "ANTHROPIC_CUSTOM_HEADERS": "x-test: 1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8192",
        },
    }
    settings_path.write_text(json.dumps(original_obj, indent=2) + "\n", encoding="utf-8")

    assert (
        ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)
        == "enabled"
    )
    parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    assert parsed["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "x-test: 1"
    assert parsed["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8192"
    for k, v in ct.TELEMETRY_ENV_KEYS.items():
        assert parsed["env"][k] == v

    assert (
        ct.sync_claude_telemetry(False, settings_path=settings_path, state_path=state_path)
        == "disabled"
    )
    assert json.loads(settings_path.read_text(encoding="utf-8")) == original_obj


def test_conflict_when_owned_key_has_different_value_leaves_file_byte_identical(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "state.json"
    original_bytes = json.dumps(
        {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.internal:4318"}},
        indent=2,
    ).encode("utf-8")
    settings_path.write_bytes(original_bytes)

    with caplog.at_level(logging.WARNING):
        status = ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)

    assert status == "conflict"
    assert settings_path.read_bytes() == original_bytes
    assert not ct.backup_path_for(settings_path).exists()
    assert not state_path.exists()
    assert len([r for r in caplog.records if "conflict" in r.message.lower()]) == 1


def test_enable_twice_is_idempotent_and_writes_one_backup(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "state.json"
    original_text = '{\n  "model": "opus"\n}\n'
    settings_path.write_text(original_text, encoding="utf-8")

    assert (
        ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)
        == "enabled"
    )
    first_after = settings_path.read_bytes()
    bak = ct.backup_path_for(settings_path)
    assert bak.read_text(encoding="utf-8") == original_text
    bak_mtime = bak.stat().st_mtime_ns

    assert (
        ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)
        == "enabled"
    )
    assert settings_path.read_bytes() == first_after
    assert bak.stat().st_mtime_ns == bak_mtime
    assert bak.read_text(encoding="utf-8") == original_text


@pytest.mark.parametrize(
    "raw_content",
    [
        b"{not valid json",
        b"[1, 2, 3]",
        b'{"env": "not-an-object"}',
    ],
)
def test_unparseable_or_malformed_file_is_left_untouched_and_reported(
    tmp_path: Path, raw_content: bytes, caplog: pytest.LogCaptureFixture
):
    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "state.json"
    settings_path.write_bytes(raw_content)

    with caplog.at_level(logging.WARNING):
        status = ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)

    assert status == "error"
    assert settings_path.read_bytes() == raw_content
    assert not ct.backup_path_for(settings_path).exists()
    assert len(caplog.records) == 1


def test_concurrent_edit_is_not_overwritten(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "state.json"
    settings_path.write_text('{"model": "sonnet"}\n', encoding="utf-8")

    original_replace = os.replace
    concurrent_bytes = b'{"model": "haiku", "concurrent": true}\n'

    def hijack_stat(path: Path):
        sig = (1, 1)
        if path == settings_path:
            # Simulate another writer changing the file before os.replace runs.
            settings_path.write_bytes(concurrent_bytes)
        return sig

    with patch.object(
        ct, "_stat_sig", side_effect=[ct._stat_sig(settings_path), hijack_stat(settings_path)]
    ):
        status = ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)

    assert status == "error"
    assert settings_path.read_bytes() == concurrent_bytes
    assert original_replace is os.replace


def test_sidecar_real_status_path_off_leaves_file_untouched_and_on_reports_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import scripts.sidecar as sidecar

    claude_settings = tmp_path / ".claude" / "settings.json"
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sidecar, "_CLAUDE_TELEMETRY_LOCAL", None)
    monkeypatch.setattr(sidecar, "_CLAUDE_TELEMETRY_SERVER", False)
    monkeypatch.setattr(sidecar, "_CLAUDE_TELEMETRY_STATUS", "disabled")
    monkeypatch.setattr(sidecar, "_TELEMETRY_SYNC_CACHE", {})
    monkeypatch.setattr(
        sidecar, "run_collection", lambda *a, **kw: sidecar.CollectionResult([], [], 0, [])
    )
    monkeypatch.setattr(sidecar, "queue_flush", lambda *a, **kw: None)

    sent_payloads: list[dict] = []
    server_responses = [
        (True, {"status": "ok", "claude_telemetry": False}, 200),
        (True, {"status": "ok", "claude_telemetry": True}, 200),
        (True, {"status": "ok", "claude_telemetry": True}, 200),
    ]

    def fake_post(url, payload, *args, **kwargs):
        sent_payloads.append(payload)
        return server_responses.pop(0)

    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", fake_post)

    runner = sidecar.DaemonRunner(
        {
            "api_url": "http://127.0.0.1:8765",
            "api_key": "test-key",  # pragma: allowlist secret
            "providers": ["all"],
        }
    )
    runner._stop_event = threading.Event()

    # Cycle 1: setting is off -> ~/.claude/settings.json untouched, status is "disabled".
    runner.run_once()
    assert sent_payloads[0]["claude_telemetry_status"] == "disabled"
    assert not claude_settings.exists()

    # Cycle 2: server turns claude_telemetry on in response -> sidecar applies it.
    runner.run_once()
    assert claude_settings.exists()
    assert json.loads(claude_settings.read_text(encoding="utf-8"))["env"] == ct.TELEMETRY_ENV_KEYS
    assert (tmp_path / "sidecar" / "claude-telemetry-state.json").exists()

    # Cycle 3: next status report carries "enabled".
    runner.run_once()
    assert sent_payloads[2]["claude_telemetry_status"] == "enabled"


def test_oserror_on_write_returns_error_and_logs_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    settings_path = tmp_path / ".claude" / "settings.json"
    state_path = tmp_path / "state" / "claude-telemetry-state.json"

    def raise_perm(*args, **kwargs):
        raise PermissionError("read-only directory")

    monkeypatch.setattr(ct, "_atomic_write_bytes", raise_perm)

    with caplog.at_level(logging.WARNING):
        status = ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)

    assert status == "error"
    assert len(caplog.records) == 1
    assert "read-only directory" in caplog.records[0].message


def test_symlinked_settings_preserves_symlink_and_updates_target(tmp_path: Path):
    dotfiles_dir = tmp_path / "dotfiles"
    dotfiles_dir.mkdir()
    real_settings = dotfiles_dir / "settings.json"
    original_obj = {"model": "sonnet"}
    real_settings.write_text(json.dumps(original_obj, indent=2) + "\n", encoding="utf-8")

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_link = claude_dir / "settings.json"
    settings_link.symlink_to(real_settings)
    state_path = tmp_path / "state.json"

    assert (
        ct.sync_claude_telemetry(True, settings_path=settings_link, state_path=state_path)
        == "enabled"
    )
    assert settings_link.is_symlink()
    assert settings_link.resolve() == real_settings.resolve()
    assert json.loads(real_settings.read_text(encoding="utf-8"))["env"] == ct.TELEMETRY_ENV_KEYS

    assert (
        ct.sync_claude_telemetry(False, settings_path=settings_link, state_path=state_path)
        == "disabled"
    )
    assert settings_link.is_symlink()
    assert json.loads(real_settings.read_text(encoding="utf-8")) == original_obj


def test_preserves_non_ascii_crlf_and_single_line_formatting(tmp_path: Path):
    # CRLF + non-ASCII round-trip
    settings_path = tmp_path / "crlf.json"
    state_path = tmp_path / "crlf-state.json"
    original_text = '{\r\n  "permissions": {\r\n    "allow": [\r\n      "Bash(echo h\u00e9llo)"\r\n    ]\r\n  }\r\n}\r\n'
    settings_path.write_bytes(original_text.encode("utf-8"))

    assert (
        ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)
        == "enabled"
    )
    enabled_bytes = settings_path.read_bytes()
    assert b"\r\n" in enabled_bytes
    assert "h\u00e9llo".encode() in enabled_bytes

    assert (
        ct.sync_claude_telemetry(False, settings_path=settings_path, state_path=state_path)
        == "disabled"
    )
    assert settings_path.read_bytes() == original_text.encode("utf-8")

    # Single-line JSON stays single-line
    single_path = tmp_path / "single.json"
    single_state = tmp_path / "single-state.json"
    single_path.write_text('{"model":"opus"}', encoding="utf-8")

    assert (
        ct.sync_claude_telemetry(True, settings_path=single_path, state_path=single_state)
        == "enabled"
    )
    assert "\n" not in single_path.read_text(encoding="utf-8")

    assert (
        ct.sync_claude_telemetry(False, settings_path=single_path, state_path=single_state)
        == "disabled"
    )
    single_after = single_path.read_text(encoding="utf-8")
    assert "\n" not in single_after
    assert json.loads(single_after) == {"model": "opus"}


def test_reestablishes_ownership_when_keys_present_and_state_file_missing(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "state.json"
    settings_path.write_text('{"model": "sonnet"}\n', encoding="utf-8")

    assert (
        ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)
        == "enabled"
    )
    state_path.unlink()

    # Re-enabling when all keys are already present re-creates the state file.
    assert (
        ct.sync_claude_telemetry(True, settings_path=settings_path, state_path=state_path)
        == "enabled"
    )
    assert state_path.exists()

    assert (
        ct.sync_claude_telemetry(False, settings_path=settings_path, state_path=state_path)
        == "disabled"
    )
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {"model": "sonnet"}


def test_sidecar_sync_skips_redundant_reads_when_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import scripts.sidecar as sidecar

    claude_settings = tmp_path / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)
    claude_settings.write_text('{"model": "sonnet"}\n', encoding="utf-8")
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sidecar, "_CLAUDE_TELEMETRY_LOCAL", None)
    monkeypatch.setattr(sidecar, "_CLAUDE_TELEMETRY_SERVER", True)
    monkeypatch.setattr(sidecar, "_CLAUDE_TELEMETRY_STATUS", "disabled")
    monkeypatch.setattr(sidecar, "_TELEMETRY_SYNC_CACHE", {})

    calls: list[bool] = []
    real_sync = ct.sync_claude_telemetry

    def spy_sync(enabled: bool, **kwargs):
        calls.append(enabled)
        return real_sync(enabled, **kwargs)

    monkeypatch.setattr(ct, "sync_claude_telemetry", spy_sync)

    assert sidecar._sync_claude_telemetry() == "enabled"
    assert sidecar._sync_claude_telemetry() == "enabled"
    assert len(calls) == 1

    # Editing settings.json invalidates the cached stat and triggers a re-sync.
    claude_settings.write_text('{"model": "opus"}\n', encoding="utf-8")
    os.utime(claude_settings, ns=(1_000_000_000, 2_000_000_000))
    assert sidecar._sync_claude_telemetry() == "enabled"
    assert len(calls) == 2


def test_fleet_registry_telemetry_statuses_match_without_sidecar_import():
    from app.services import fleet_registry

    assert fleet_registry.CLAUDE_TELEMETRY_STATUSES == ct.VALID_STATUSES
