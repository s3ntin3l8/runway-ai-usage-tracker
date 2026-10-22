"""Renewal protocol tests use only an in-memory Keychain."""

import copy
import json
from contextlib import contextmanager

import pytest

from scripts.sidecar_pkg import claude_code_keychain as discovery
from scripts.sidecar_pkg import claude_keychain_renewer as renewal
from scripts.sidecar_pkg import keychain_client
from scripts.sidecar_pkg.macos_keychain_store import KeychainError, KeychainItem


class MemoryStore:
    def __init__(self):
        self.data = {
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 1,
            },
            "mcpOAuth": {"preserve": True},
            "unknown": [1, 2],
        }
        self.journal = None
        self.fail_save = False
        self.fail_write = False
        self.fail_delete = False
        self.saved = []
        self.writes = 0
        self.missing = False
        self.service = "Claude Code-credentials"

    def find(self, service, account):
        if service != self.service or self.missing:
            return None
        return self.read(b"exact")

    def read(self, reference):
        if self.missing:
            raise KeychainError("missing")
        assert reference == b"exact"
        return KeychainItem(reference, json.dumps(self.data).encode(), self.service, "test-user")

    def read_journal(self, reference):
        return copy.deepcopy(self.journal)

    def save_journal(self, reference, value):
        if self.fail_save:
            raise KeychainError("unavailable")
        self.journal = copy.deepcopy(value)
        self.saved.append(copy.deepcopy(value))

    def delete_journal(self, reference):
        if self.fail_delete:
            raise KeychainError("failed")
        self.journal = None

    def check_update(self, reference):
        return True

    def update(self, reference, data):
        if self.fail_write:
            raise KeychainError("failed")
        self.writes += 1
        self.data = json.loads(data)


@contextmanager
def unlocked():
    yield True


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(renewal.sys, "platform", "darwin")
    monkeypatch.setattr(renewal, "account_name", lambda: "test-user")
    monkeypatch.setattr(renewal, "claude_renewal_lock", unlocked)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    store = MemoryStore()
    calls = []

    def refresh(*args):
        calls.append(args)
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 28800}

    monkeypatch.setattr(renewal, "request_refresh", refresh)

    def make(allowed=lambda: True):
        return renewal.KeychainRenewer(lambda: [], allowed, lambda: store)

    return store, calls, make


def test_renewal_preserves_json_and_journals_before_write(env):
    store, calls, make = env
    assert make().renew()
    assert len(calls) == 1
    assert [r["phase"] for r in store.saved] == ["intent", "pending"]
    assert store.data["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert store.data["mcpOAuth"] == {"preserve": True}
    assert store.data["unknown"] == [1, 2]
    assert store.data["claudeAiOauth"]["expiresAt"] > 1
    assert store.journal is None


@pytest.mark.parametrize("permission", [False, "true", 1, None])
def test_only_literal_local_true_allows_any_access(env, permission):
    store, calls, make = env
    assert make(lambda: permission).renew()
    assert not calls and not store.saved


@pytest.mark.parametrize("expiry", [None, "1", True, float("nan"), float("inf")])
def test_invalid_expiry_never_refreshes(env, expiry):
    store, calls, make = env
    store.data["claudeAiOauth"]["expiresAt"] = expiry
    make().renew()
    assert not calls


def test_not_due_and_missing_are_noops(env):
    store, calls, make = env
    store.data["claudeAiOauth"]["expiresAt"] = 4102444800000
    make().renew()
    store.missing = True
    make().renew()
    assert not calls


def test_failed_intent_save_prevents_exchange(env):
    store, calls, make = env
    store.fail_save = True
    assert not make().renew()
    assert not calls


def test_pending_survives_restart_without_second_exchange(env):
    store, calls, make = env
    store.fail_write = True
    assert not make().renew()
    assert store.journal["phase"] == "pending"
    store.fail_write = False
    assert make().renew()
    assert len(calls) == 1
    assert store.journal is None


def test_write_succeeded_but_cleanup_failed_does_not_refresh_again(env):
    store, calls, make = env
    store.fail_delete = True
    assert not make().renew()
    store.fail_delete = False
    assert make().renew()
    assert len(calls) == 1 and store.writes == 1
    assert store.journal is None


def test_interrupted_intent_blocks_reuse_until_login_changes(env):
    store, calls, make = env
    store.journal = {"phase": "intent", "refresh": "old-refresh"}
    assert not make().renew()
    assert not calls
    store.data["claudeAiOauth"]["refreshToken"] = "independent-login"
    assert make().renew()
    assert len(calls) == 1


def test_network_timeout_is_ambiguous_and_never_retried(env, monkeypatch):
    store, calls, make = env

    def timeout(*args):
        calls.append(args)
        raise OSError("private-response")

    monkeypatch.setattr(renewal, "request_refresh", timeout)
    assert not make().renew()
    assert not make().renew()
    assert len(calls) == 1


def test_concurrent_new_login_is_preserved(env, monkeypatch):
    store, calls, make = env

    def refresh(*args):
        calls.append(args)
        store.data["claudeAiOauth"]["refreshToken"] = "concurrent-login"
        return {"access_token": "new-access", "refresh_token": "new-refresh"}

    monkeypatch.setattr(renewal, "request_refresh", refresh)
    assert make().renew()
    assert store.data["claudeAiOauth"]["refreshToken"] == "concurrent-login"
    assert store.writes == 0


def test_permission_revocation_during_exchange_prevents_writes(env, monkeypatch):
    store, calls, make = env
    permission = [True]

    def refresh(*args):
        permission[0] = False
        return {"access_token": "new-access", "refresh_token": "new-refresh"}

    monkeypatch.setattr(renewal, "request_refresh", refresh)
    assert not make(lambda: permission[0]).renew()
    assert store.writes == 0
    assert store.journal["phase"] == "intent"


def test_denial_shared_with_discovery_and_logs_sanitized(env, caplog):
    store, calls, make = env

    def deny(*args):
        raise KeychainError("denied")

    store.find = deny
    assert not make().renew()
    assert discovery.access_blocked()
    assert make().renew()
    assert not calls
    assert "old-refresh" not in caplog.text
    assert "test-user" not in caplog.text


def test_suffix_targets_follow_secure_storage_override(env, monkeypatch):
    import hashlib

    store, calls, make = env
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/synthetic/work")
    store.service += "-" + hashlib.sha256(b"/synthetic/work").hexdigest()[:8]
    assert make().renew()
    assert len(calls) == 1


def test_failed_response_journal_retries_save_in_memory(env, monkeypatch):
    store, calls, make = env
    refresh = renewal.request_refresh

    def fail_after_exchange(*args):
        response = refresh(*args)
        store.fail_save = True
        return response

    monkeypatch.setattr(renewal, "request_refresh", fail_after_exchange)
    worker = make()
    assert not worker.renew()
    assert len(calls) == 1 and store.journal["phase"] == "intent"
    store.fail_save = False
    monkeypatch.setattr(keychain_client, "_CLIENT", None)
    assert worker.renew()
    assert len(calls) == 1
    assert store.data["claudeAiOauth"]["refreshToken"] == "new-refresh"


def test_failed_response_journal_restart_blocks_second_exchange(env, monkeypatch):
    store, calls, make = env
    refresh = renewal.request_refresh

    def fail_after_exchange(*args):
        response = refresh(*args)
        store.fail_save = True
        return response

    monkeypatch.setattr(renewal, "request_refresh", fail_after_exchange)
    assert not make().renew()
    store.fail_save = False
    monkeypatch.setattr(keychain_client, "_CLIENT", None)
    assert not make().renew()
    assert len(calls) == 1


def test_revocation_preserves_pending_journal_until_reenabled(env):
    store, calls, make = env
    store.fail_write = True
    assert not make().renew()
    pending = copy.deepcopy(store.journal)
    assert make(lambda: False).renew()
    assert store.journal == pending
    store.fail_write = False
    assert make().renew()
    assert len(calls) == 1


def test_target_removed_during_exchange_is_never_recreated(env, monkeypatch):
    store, calls, make = env
    refresh = renewal.request_refresh

    def remove_target(*args):
        response = refresh(*args)
        store.missing = True
        return response

    monkeypatch.setattr(renewal, "request_refresh", remove_target)
    assert not make().renew()
    assert store.writes == 0
    assert store.journal["phase"] == "pending"
    assert make().renew()
    assert len(calls) == 1


def test_unrelated_changes_during_exchange_survive(env, monkeypatch):
    store, calls, make = env
    refresh = renewal.request_refresh

    def concurrent_change(*args):
        store.data["mcpOAuth"] = {"changed": True}
        store.data["claudeAiOauth"]["unknownField"] = "keep"
        return refresh(*args)

    monkeypatch.setattr(renewal, "request_refresh", concurrent_change)
    assert make().renew()
    assert store.data["mcpOAuth"] == {"changed": True}
    assert store.data["claudeAiOauth"]["unknownField"] == "keep"


def test_rejection_is_persisted_and_not_retried_after_restart(env, monkeypatch, caplog):
    store, calls, make = env

    def reject(*args):
        calls.append(args)
        raise renewal.RefreshRejectedError("private-provider-body")

    monkeypatch.setattr(renewal, "request_refresh", reject)
    assert not make().renew()
    assert not make().renew()
    assert len(calls) == 1 and store.journal["phase"] == "rejected"
    assert "private-provider-body" not in caplog.text


def test_login_rotated_while_persisting_intent_is_not_exchanged(env):
    store, calls, make = env
    save = store.save_journal

    def concurrent_rotation(reference, record):
        save(reference, record)
        if record["phase"] == "intent":
            store.data["claudeAiOauth"]["refreshToken"] = "concurrent-login"

    store.save_journal = concurrent_rotation
    assert make().renew()
    assert not calls
    assert store.journal is None


def test_unverified_write_retains_recovery_and_never_reports_success(env, caplog):
    store, calls, make = env
    store.update = lambda *args: None
    assert not make().renew()
    assert store.journal["phase"] == "pending"
    assert "renewed" not in caplog.text


def test_missing_or_invalid_refresh_response_leaves_ambiguous_intent(env, monkeypatch):
    store, calls, make = env

    def malformed(*args):
        calls.append(args)
        return {"access_token": "new-access"}

    monkeypatch.setattr(renewal, "request_refresh", malformed)
    assert not make().renew()
    assert not make().renew()
    assert len(calls) == 1
    assert store.writes == 0


def test_nonrotating_response_saves_replacement_access_token(env, monkeypatch):
    store, calls, make = env
    refresh = renewal.request_refresh

    def reuse(*args):
        response = refresh(*args)
        response["refresh_token"] = "old-refresh"
        return response

    monkeypatch.setattr(renewal, "request_refresh", reuse)
    assert make().renew()
    assert store.writes == 1
    assert store.data["claudeAiOauth"]["accessToken"] == "new-access"
    assert store.data["claudeAiOauth"]["refreshToken"] == "old-refresh"
    assert len(calls) == 1 and store.journal is None


def test_nonrotating_write_with_failed_cleanup_never_exchanges_again(env, monkeypatch):
    store, calls, make = env
    refresh = renewal.request_refresh

    def reuse(*args):
        response = refresh(*args)
        response["refresh_token"] = "old-refresh"
        return response

    monkeypatch.setattr(renewal, "request_refresh", reuse)
    worker = make()
    store.fail_delete = True
    assert not worker.renew()
    assert store.writes == 1
    store.fail_delete = False
    assert worker.renew()
    assert store.writes == 1 and len(calls) == 1 and store.journal is None


def test_unsupported_partition_policy_blocks_exchange_and_intent(env, caplog):
    store, calls, make = env
    store.check_update = lambda ref: False
    worker = make()
    assert not worker.renew()
    assert not worker.renew()
    assert not calls and not store.saved and store.writes == 0
    messages = [r.getMessage() for r in caplog.records if "access policy" in r.getMessage()]
    assert len(messages) == 1
    assert "old-refresh" not in " ".join(messages)


def test_policy_change_after_intent_blocks_exchange_and_clears_unspent_intent(env):
    store, calls, make = env
    checks = iter([True, False])
    store.check_update = lambda ref: next(checks)
    assert not make().renew()
    assert not calls and store.journal is None and store.writes == 0


def test_pending_replacement_waits_for_safe_policy_without_another_exchange(env):
    store, calls, make = env
    store.fail_write = True
    worker = make()
    assert not worker.renew()
    assert len(calls) == 1
    store.fail_write = False
    store.check_update = lambda ref: False
    restarted = make()
    assert not restarted.renew()
    assert len(calls) == 1 and store.journal["phase"] == "pending"
    store.check_update = lambda ref: True
    assert restarted.renew()
    assert len(calls) == 1 and store.journal is None
