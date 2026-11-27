"""Authorization follows secret-free read outcomes, including across restarts."""

import contextlib
import json
import os
import time
from collections.abc import Iterator
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

    payload = SourceClient(local, tmp_path / "control.json").payload(
        local, source_settings.report(local)
    )
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


@pytest.mark.parametrize(
    "reason", ["denied", "timed_out", "unavailable", "missing", "identity changed", "ambiguous"]
)
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


@pytest.mark.parametrize("outcome", ["empty", "missing", "ambiguous", "identity changed"])
@pytest.mark.parametrize("source", [SOURCE, "anthropic.chrome"])
def test_background_reader_failure_replaces_restart_evidence(
    local, tmp_path, monkeypatch, outcome, source
):
    from scripts.sidecar_pkg import claude_code_keychain, macos_cookies, macos_keychain_store
    from scripts.sidecar_pkg.keychain_protocol import KeychainError
    from scripts.sidecar_pkg.source_catalog_data import KEYCHAIN_RULES

    config = SourcePolicy.enable_all(local)
    policy = SourcePolicy(config)
    path = tmp_path / "reads.json"
    policy.configure_state(path)
    policy.record_read(policy.ticket(source))
    restarted = SourcePolicy(config)
    restarted.configure_state(path)
    monkeypatch.setattr(source_consent, "POLICY", restarted)
    monkeypatch.setattr(_WorkerAPI, "call", WORKER_CALL)
    service = "Claude Code-credentials" if source == SOURCE else "Chrome Safe Storage"
    item = {"service": service, "account": "synthetic-user", "persistent": b"synthetic-ref"}

    def call(operation, query, values, **kwargs):
        if "service" in query:
            if outcome == "missing":
                raise KeychainError("missing")
            return [] if outcome == "empty" else [item] * (2 if outcome == "ambiguous" else 1)
        return {**item, "service": "wrong-service", "data": b"synthetic-secret"}

    client = Mock()
    client.call.side_effect = call
    monkeypatch.setattr(keychain_client, "get_keychain_client", lambda: client)
    monkeypatch.setattr(macos_keychain_store.sys, "platform", "darwin")
    monkeypatch.setattr(claude_code_keychain, "service_candidates", lambda _: [service])
    monkeypatch.setattr(claude_code_keychain, "account_name", lambda: "synthetic-user")
    monkeypatch.setattr(macos_cookies, "_KEY_CACHE", {})
    monkeypatch.setattr(macos_cookies, "_KEY_STATUS", {})
    rule = next(r for r in KEYCHAIN_RULES["anthropic"]["rules"] if r["type"] == "cookie")
    with source_consent.rule_scope("anthropic", rule):
        if source == SOURCE:
            assert claude_code_keychain.read_candidates([], {}) == []
        else:
            assert macos_cookies.storage_key(service) is None
    reported = next(r for r in source_settings.report(config)["sources"] if r["id"] == source)
    assert reported["state"] == "Action required"
    saved = json.loads(path.read_text())[source]
    assert saved["last_block_reason"] == ("missing" if outcome == "empty" else outcome)
    assert saved["last_block_at"] >= saved["last_read_at"]
    restarted = SourcePolicy(config)
    restarted.configure_state(path)
    assert restarted.read_state(source) == "Action required"


@pytest.mark.parametrize("failure", ["launch", "verify", "exception"])
def test_failed_setup_replaces_success_and_persists(local, tmp_path, monkeypatch, failure):
    path = tmp_path / "reads.json"
    policy = source_consent.POLICY
    policy.configure_state(path)
    policy.record_read(policy.ticket(SOURCE))
    controller = source_setup.SetupController(
        launch=lambda _: failure != "launch",
        verify=Mock(
            side_effect=RuntimeError("private") if failure == "exception" else None,
            return_value=False,
        ),
    )
    monkeypatch.setattr(source_setup, "SETUP", controller)
    controller.start(SOURCE)
    controller.wait(2)
    assert row(local)["state"] == "Action required"
    restarted = SourcePolicy(local)
    restarted.configure_state(path)
    assert restarted.read_state(SOURCE) == "Action required"


def test_missing_optional_recovery_record_does_not_invalidate_read(local, monkeypatch):
    from scripts.sidecar_pkg.keychain_protocol import KeychainError
    from scripts.sidecar_pkg.macos_keychain_store import RECOVERY_SERVICE

    policy = source_consent.POLICY
    policy.record_read(policy.ticket(SOURCE))
    client = Mock()
    client.call.side_effect = KeychainError("missing")
    monkeypatch.setattr(keychain_client, "get_keychain_client", lambda: client)
    with pytest.raises(KeychainError, match="missing"):
        WORKER_CALL(_WorkerAPI(), "copy", {"service": RECOVERY_SERVICE}, {})
    assert row(local)["state"] == "Authorized"


@contextlib.contextmanager
def _unlocked() -> Iterator[bool]:
    yield True


@pytest.mark.parametrize(
    "outcome",
    ["missing-first", "missing-last", "all-missing", "denied", "timed_out", "unavailable"],
)
@pytest.mark.parametrize("scan", ["collection", "keep-alive"])
def test_claude_candidate_scan_reports_source_outcome(local, tmp_path, monkeypatch, outcome, scan):
    from scripts.sidecar_pkg import (
        claude_code_keychain,
        claude_keychain_renewer,
        macos_keychain_store,
    )
    from scripts.sidecar_pkg.keychain_protocol import KeychainError

    policy = source_consent.POLICY
    path = tmp_path / "reads.json"
    policy.configure_state(path)
    policy.record_read(policy.ticket(SOURCE))
    restarted = SourcePolicy(local)
    restarted.configure_state(path)
    monkeypatch.setattr(source_consent, "POLICY", restarted)
    monkeypatch.setattr(_WorkerAPI, "call", WORKER_CALL)
    # ``sys`` is shared, so this makes the whole process look like macOS.
    monkeypatch.setattr(macos_keychain_store.sys, "platform", "darwin")
    # The renewal lock would then take the macOS ``fcntl`` file lock, which does not exist
    # on Windows; it is not under test here (see ``test_claude_renewal_lock.py``).
    monkeypatch.setattr(claude_keychain_renewer, "claude_renewal_lock", _unlocked)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(claude_code_keychain, "account_name", lambda: "synthetic-user")
    monkeypatch.setattr(claude_keychain_renewer, "account_name", lambda: "synthetic-user")
    services = claude_code_keychain.service_candidates([tmp_path / "extra"])
    present = services[1] if outcome == "missing-first" else services[0]
    item = {"service": present, "account": "synthetic-user", "persistent": b"synthetic-ref"}

    def call(operation, query, values, **kwargs):
        if "service" in query:
            if query["service"] == macos_keychain_store.RECOVERY_SERVICE:
                raise KeychainError("missing")
            if query["service"] == present and outcome != "all-missing":
                return [item]
            raise KeychainError(
                outcome if outcome in {"denied", "timed_out", "unavailable"} else "missing"
            )
        return {
            **item,
            "data": json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "synthetic-token",
                        "refreshToken": "synthetic-refresh",
                        "expiresAt": 4102444800000,
                    }
                }
            ).encode(),
        }

    client = Mock()
    client.access_blocked.return_value = False
    client.call.side_effect = call
    monkeypatch.setattr(keychain_client, "get_keychain_client", lambda: client)
    if scan == "collection":
        candidates = claude_code_keychain.read_candidates(
            [tmp_path / "extra"], {"claudeAiOauth.accessToken": "oauth_token"}
        )
        assert len(candidates) == (0 if outcome == "all-missing" else 1)
    else:
        worker = claude_keychain_renewer.KeychainRenewer(lambda: [tmp_path / "extra"], lambda: True)
        assert worker.renew() == (outcome not in {"denied", "timed_out", "unavailable"})
    successful = outcome in {"missing-first", "missing-last"}
    assert row(local)["state"] == ("Authorized" if successful else "Action required")
    saved = json.loads(path.read_text())[SOURCE]
    assert row(local)["last_read_at"] == saved["last_read_at"]
    assert saved["last_block_reason"] == ("missing" if "missing" in outcome else outcome)
    if successful:
        assert saved["last_read_at"] > saved["last_block_at"]
    else:
        assert saved["last_block_at"] >= saved["last_read_at"]
    assert "synthetic-token" not in path.read_text()
    assert "synthetic-refresh" not in path.read_text()
    assert "synthetic-user" not in path.read_text()
    persisted = SourcePolicy(local)
    persisted.configure_state(path)
    state = persisted.read_state(SOURCE)
    if successful:
        assert state.startswith("Authorized (checked ")
    else:
        assert state == "Action required"
