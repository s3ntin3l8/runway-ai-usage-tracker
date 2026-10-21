"""Explicit foreground consent is isolated from all background credential IO."""

import importlib
from unittest.mock import Mock

import pytest

from scripts.sidecar_pkg.keychain_protocol import OPERATIONS, KeychainError


@pytest.fixture
def authorization(monkeypatch):
    module = importlib.import_module("scripts.sidecar_pkg.keychain_authorization")
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    return module


def fake_api():
    item = {"persistent": b"exact", "service": "service", "account": "local-user"}
    api = Mock()
    api.call.return_value = [item]
    api.authorize_read.return_value = {**item, "data": b"synthetic-secret-never-print"}
    return api


def test_exact_selection_only_one_foreground_read(authorization):
    api = fake_api()
    authorization.authorize_once(api, ["service"], "local-user")
    api.call.assert_called_once_with("copy", {"service": "service", "account": "local-user"}, {})
    api.authorize_read.assert_called_once_with(b"exact")
    assert "authorize" not in OPERATIONS


@pytest.mark.parametrize("matches", [[], [{"persistent": b"one"}, {"persistent": b"two"}]])
def test_missing_or_ambiguous_has_no_interactive_read(authorization, matches):
    api = fake_api()
    api.call.return_value = matches
    with pytest.raises(KeychainError):
        authorization.authorize_once(api, ["service"], "local-user")
    api.authorize_read.assert_not_called()


def test_cross_service_ambiguity_never_prompts(authorization):
    api = fake_api()
    with pytest.raises(KeychainError, match="ambiguous"):
        authorization.authorize_once(api, ["service", "suffixed"], "local-user")
    api.authorize_read.assert_not_called()


@pytest.mark.parametrize(
    "field,value", [("service", "other"), ("account", "other"), ("persistent", "bad")]
)
def test_invalid_selection_fails_before_consent(authorization, field, value):
    api = fake_api()
    api.call.return_value[0][field] = value
    with pytest.raises(KeychainError):
        authorization.authorize_once(api, ["service"], "local-user")
    api.authorize_read.assert_not_called()


def test_changed_item_after_consent_is_not_success(authorization):
    api = fake_api()
    api.authorize_read.return_value["account"] = "changed"
    with pytest.raises(KeychainError, match="identity changed"):
        authorization.authorize_once(api, ["service"], "local-user")
    assert api.authorize_read.call_count == 1


def test_denial_stops_selection_without_fallback_or_retry(authorization):
    api = fake_api()
    api.call.side_effect = KeychainError("denied")
    with pytest.raises(KeychainError, match="denied"):
        authorization.authorize_once(api, ["service", "other"], "local-user")
    assert api.call.call_count == 1
    api.authorize_read.assert_not_called()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_non_darwin_never_initializes_native(authorization, monkeypatch, platform, capsys):
    factory = Mock()
    monkeypatch.setattr(authorization, "_NativeAPI", factory)
    monkeypatch.setattr(authorization.sys, "platform", platform)
    with pytest.raises(SystemExit) as caught:
        authorization.authorization_command(["--authorize-claude-keychain"])
    assert caught.value.code == 1
    factory.assert_not_called()
    assert "macOS" in capsys.readouterr().err


def test_unrelated_or_invalid_arguments_do_not_initialize_native(authorization, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(authorization, "_NativeAPI", factory)
    assert authorization.authorization_command([]) is False
    with pytest.raises(SystemExit) as caught:
        authorization.authorization_command(["--authorize-claude-keychain", "unexpected"])
    assert caught.value.code == 2
    factory.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        None,
        KeychainError("denied", native_status=-25308),
        RuntimeError("synthetic-secret-never-print"),
    ],
)
def test_command_reports_sanitized_outcome_and_cancels_watchdog(
    authorization, monkeypatch, capsys, error
):
    operation = Mock(side_effect=error)
    monkeypatch.setattr(authorization, "authorize_once", operation)
    monkeypatch.setattr(authorization, "_NativeAPI", Mock())
    timer = Mock()
    monkeypatch.setattr(authorization.threading, "Timer", Mock(return_value=timer))
    with pytest.raises(SystemExit) as caught:
        authorization.authorization_command(["--authorize-claude-keychain"])
    assert caught.value.code == (1 if error else 0)
    output = capsys.readouterr()
    assert "synthetic-secret-never-print" not in output.out + output.err
    if isinstance(error, KeychainError):
        assert "-25308" in output.err
    timer.start.assert_called_once()
    timer.cancel.assert_called_once()
    assert operation.call_count == 1


def test_environment_service_override_uses_existing_derivation(authorization, monkeypatch):
    operation = Mock()
    monkeypatch.setattr(authorization, "authorize_once", operation)
    monkeypatch.setattr(authorization, "_NativeAPI", Mock())
    monkeypatch.setattr(authorization.threading, "Timer", Mock())
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "synthetic-directory")
    with pytest.raises(SystemExit):
        authorization.authorization_command(["--authorize-claude-keychain"])
    services = operation.call_args.args[1]
    assert len(services) == 1 and services[0].startswith("Claude Code-credentials-")


def test_watchdog_reports_no_details_and_exits(authorization, monkeypatch, capsys):
    terminate = Mock()
    monkeypatch.setattr(authorization.os, "_exit", terminate)
    authorization._timed_out()
    terminate.assert_called_once_with(1)
    assert capsys.readouterr().err == "Claude Code Keychain: authorization timed out\n"


@pytest.mark.parametrize("entry", ["sidecar_app/__main__.py", "scripts/sidecar.py"])
def test_authorization_help_exits_before_desktop_or_native_initialization(entry):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / entry), "--authorize-claude-keychain", "--help"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "Authorize one Claude Code Keychain read" in result.stdout
