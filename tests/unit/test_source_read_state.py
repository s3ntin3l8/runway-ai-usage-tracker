"""Authorization follows secret-free read outcomes, including across restarts."""

import json
import os
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest

from scripts.sidecar_pkg import keychain_client, source_consent, source_settings, source_setup
from scripts.sidecar_pkg.macos_keychain_store import _WorkerAPI
from scripts.sidecar_pkg.source_consent import SourcePolicy

WORKER_CALL = _WorkerAPI.call

SOURCE = "anthropic.claude-code"


@pytest.fixture
def local(monkeypatch):
    config = SourcePolicy.change({"api_url": "https://synthetic.example"}, SOURCE, read=True)
    monkeypatch.setattr(source_consent, "POLICY", SourcePolicy(config))
    monkeypatch.setattr(keychain_client, "_CLIENT", None)
    monkeypatch.setattr(source_setup, "SETUP", source_setup.SetupController())
    monkeypatch.setattr(source_settings, "_safari_report", lambda _: {})
    return config


def row(config):
    return next(r for r in source_settings.report(config)["sources"] if r["id"] == SOURCE)


def test_background_read_authorizes_without_setup(local):
    policy = source_consent.POLICY
    policy.record_read(policy.ticket(SOURCE))
    assert row(local)["state"] == "Authorized"
    assert row(local)["last_read_at"] == policy.last_read(SOURCE)


def test_restart_recent_success_and_fleet_payload(local, tmp_path, monkeypatch):
    from scripts.sidecar_pkg.source_protocol import SourceClient

    path = tmp_path / "source-reads.json"
    policy = source_consent.POLICY
    policy.configure_state(path)
    policy.record_read(policy.ticket(SOURCE))
    last = policy.last_read(SOURCE)
    restarted = SourcePolicy(local)
    restarted.configure_state(path)
    monkeypatch.setattr(source_consent, "POLICY", restarted)
    assert row(local)["state"].startswith("Authorized (checked ")
    assert row(local)["last_read_at"] == last
    monkeypatch.setattr(source_settings.sys, "platform", "darwin")
    from app.models.source_controls import SourceSyncRequest

    payload = SourceClient(local, tmp_path / "control.json").payload(local)
    SourceSyncRequest.model_validate(payload)
    reported = next(r for r in payload["sources"] if r["source_id"] == SOURCE)
    assert reported["state"] == row(local)["state"]
    assert reported["last_read_at"] == last
    restarted.record_read(restarted.ticket(SOURCE))
    assert row(local)["state"] == "Authorized"


@pytest.mark.parametrize("age", [8 * 86400, -3600])
def test_stale_or_future_success_requires_setup(local, tmp_path, monkeypatch, age):
    policy = source_consent.POLICY
    path = tmp_path / "reads.json"
    policy.configure_state(path)
    with monkeypatch.context() as patch:
        now = time.time()
        patch.setattr(source_consent.time, "time", lambda: now - age)
        policy.record_read(policy.ticket(SOURCE))
    restarted = SourcePolicy(local)
    restarted.configure_state(path)
    monkeypatch.setattr(source_consent, "POLICY", restarted)
    assert row(local)["state"] == "Setup required"


@pytest.mark.parametrize("reason", ["denied", "timed_out", "unavailable"])
def test_block_overrides_success_and_persists(local, tmp_path, monkeypatch, reason):
    from scripts.sidecar_pkg.keychain_protocol import KeychainError

    path = tmp_path / "reads.json"
    policy = source_consent.POLICY
    policy.configure_state(path)
    policy.record_read(policy.ticket(SOURCE))
    client = Mock()
    client.call.side_effect = KeychainError(reason)
    monkeypatch.setattr(keychain_client, "get_keychain_client", lambda: client)
    with pytest.raises(KeychainError):
        WORKER_CALL(_WorkerAPI(), "copy", {"service": "Claude Code-credentials"}, {})
    assert row(local)["state"] == "Action required"
    saved = json.loads(path.read_text())[SOURCE]
    assert saved["last_block_reason"] == reason
    assert saved["last_block_at"] > 0
    restarted = SourcePolicy(local)
    restarted.configure_state(path)
    monkeypatch.setattr(source_consent, "POLICY", restarted)
    assert row(local)["state"] == "Action required"
    restarted.record_read(restarted.ticket(SOURCE))
    assert row(local)["state"] == "Authorized"


def test_record_is_owner_only_and_contains_only_allowlisted_metadata(local, tmp_path):
    path = tmp_path / "reads.json"
    policy = source_consent.POLICY
    policy.configure_state(path)
    policy.record_read(policy.ticket(SOURCE))
    saved = json.loads(path.read_text())
    assert saved == {
        SOURCE: {
            "revision": policy.ticket(SOURCE).revision,
            "last_read_at": policy.last_read(SOURCE),
            "last_block_reason": None,
            "last_block_at": None,
        }
    }
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    assert not any(
        word in path.read_text()
        for word in (
            "secret",
            "token",
            "cookie",
            "account",
            "synthetic.example",
            "generation",
            "destination",
        )
    )


def test_descriptor_revision_invalidates_saved_success(local, tmp_path, monkeypatch):
    path = tmp_path / "reads.json"
    policy = source_consent.POLICY
    policy.configure_state(path)
    policy.record_read(policy.ticket(SOURCE))
    original = source_consent.catalog()
    changed = {**original, SOURCE: replace(original[SOURCE], revision="new-revision")}
    monkeypatch.setattr(source_consent, "catalog", lambda: changed)
    updated = SourcePolicy.change(local, SOURCE, read=True)
    restarted = SourcePolicy(updated)
    restarted.configure_state(path)
    monkeypatch.setattr(source_consent, "POLICY", restarted)
    assert row(updated)["state"] == "Setup required"
    assert restarted.last_read(SOURCE) is None


@pytest.mark.parametrize("kind", ["corrupt", "malformed", "unsafe", "unreadable", "symlink"])
def test_untrusted_state_fails_closed(local, tmp_path, monkeypatch, kind):
    if os.name == "nt" and kind in {"unsafe", "symlink"}:
        pytest.skip("POSIX permissions and unprivileged symlinks are macOS/Linux checks")
    path = tmp_path / "reads.json"
    policy = source_consent.POLICY
    policy.configure_state(path)
    policy.record_read(policy.ticket(SOURCE))
    if kind == "corrupt":
        path.write_text("{")
    elif kind == "malformed":
        path.write_text(json.dumps({SOURCE: {"last_read_at": time.time()}}))
    elif kind == "unsafe":
        path.chmod(0o644)
    elif kind == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    else:
        monkeypatch.setattr(type(path), "read_text", Mock(side_effect=PermissionError))
    restarted = SourcePolicy(local)
    restarted.configure_state(path)
    monkeypatch.setattr(source_consent, "POLICY", restarted)
    assert row(local)["state"] == "Setup required"


def test_background_worker_read_persists_only_metadata(local, tmp_path, monkeypatch):
    path = tmp_path / "reads.json"
    policy = source_consent.POLICY
    policy.configure_state(path)
    client = Mock()
    client.call.return_value = {
        "service": "Claude Code-credentials",
        "data": b"synthetic-secret-token-cookie",
        "account": "synthetic-account@example.com",
        "persistent": b"synthetic-private-reference",
    }
    monkeypatch.setattr(keychain_client, "get_keychain_client", lambda: client)
    result = WORKER_CALL(_WorkerAPI(), "copy", {"service": "Claude Code-credentials"}, {})
    assert result["data"] == b"synthetic-secret-token-cookie"
    assert row(local)["state"] == "Authorized"
    assert not any(
        value in path.read_text()
        for value in (
            "synthetic-secret-token-cookie",
            "synthetic-account@example.com",
            "synthetic-private-reference",
        )
    )


def test_changed_consent_cannot_reuse_read_evidence(local, tmp_path):
    policy = source_consent.POLICY
    path = tmp_path / "reads.json"
    policy.configure_state(path)
    ticket = policy.ticket(SOURCE)
    policy.record_read(ticket)
    off = SourcePolicy.change(local, SOURCE, read=False)
    policy.configure(off)
    policy.record_read(ticket)
    policy.record_block(ticket, "denied")
    enabled = SourcePolicy.change(off, SOURCE, read=True)
    policy.configure(enabled)
    assert policy.read_state(SOURCE) is None
    assert policy.last_read(SOURCE) is None
    assert json.loads(path.read_text()) == {}


def test_unwritable_state_does_not_break_collection(local, tmp_path, monkeypatch):
    policy = source_consent.POLICY
    path = tmp_path / "reads.json"
    policy.configure_state(path)
    monkeypatch.setattr(source_consent.tempfile, "mkstemp", Mock(side_effect=PermissionError))
    policy.record_read(policy.ticket(SOURCE))
    assert row(local)["state"] == "Authorized"
    policy.record_block(policy.ticket(SOURCE), "denied")
    assert row(local)["state"] == "Action required"
    assert not path.exists()
