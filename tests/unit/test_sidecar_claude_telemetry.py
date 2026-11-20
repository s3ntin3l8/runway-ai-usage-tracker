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
