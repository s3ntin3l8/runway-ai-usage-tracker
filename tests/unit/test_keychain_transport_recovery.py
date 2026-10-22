"""Actual renewal and journal adapter across the real pipe protocol; synthetic data only."""

import json
import plistlib
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.sidecar_pkg import claude_keychain_renewer as renewal
from scripts.sidecar_pkg import keychain_client as clients
from scripts.sidecar_pkg import macos_keychain_store as store

# Captured before the suite-wide guard; restored only with a synthetic client command.
WORKER_CALL = store._WorkerAPI.call
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Darwin transport uses POSIX pipes")


@contextmanager
def unlocked():
    yield True


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    snapshot = tmp_path / "synthetic-store.plist"
    fault = tmp_path / "synthetic-fault"
    snapshot.write_bytes(
        plistlib.dumps(
            {
                "target": {
                    "persistent": b"target",
                    "service": "Claude Code-credentials",
                    "account": "synthetic-user",
                    "data": json.dumps(
                        {
                            "claudeAiOauth": {
                                "accessToken": "synthetic-old-access",
                                "refreshToken": "synthetic-old-refresh",
                                "expiresAt": 1,
                            },
                            "unrelated": "preserve",
                        }
                    ).encode(),
                }
            }
        )
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "keychain_worker_fixture.py"),
        str(snapshot),
        str(fault),
    ]
    active = []
    exchanges = []
    monkeypatch.setattr(store._WorkerAPI, "call", WORKER_CALL)
    monkeypatch.setattr(renewal.sys, "platform", "darwin")
    monkeypatch.setattr(renewal, "account_name", lambda: "synthetic-user")
    monkeypatch.setattr(renewal, "claude_renewal_lock", unlocked)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

    def exchange(*args):
        exchanges.append(True)
        return {
            "access_token": "synthetic-new-access",
            "refresh_token": "synthetic-new-refresh",
            "expires_in": 28800,
        }

    monkeypatch.setattr(renewal, "request_refresh", exchange)

    def restart():
        for client in active:
            client.close()
        client = clients.KeychainClient(command=command, timeout=2)
        active.append(client)
        monkeypatch.setattr(clients, "_CLIENT", client)
        return renewal.KeychainRenewer(lambda: [], lambda: True)

    yield restart, snapshot, fault, exchanges
    for client in active:
        client.close()


@pytest.mark.parametrize(
    "mode,recovered",
    [
        ("pending_before_save", False),
        ("pending_after_save", True),
        ("target_after_save", True),
        ("cleanup_after_save", True),
    ],
)
def test_restart_after_worker_loss_never_exchanges_pending_token_again(runtime, mode, recovered):
    restart, snapshot, fault, exchanges = runtime
    worker = restart()
    fault.write_text(mode)
    assert not worker.renew()
    assert len(exchanges) == 1
    worker = restart()
    assert worker.renew() is recovered
    assert len(exchanges) == 1
    items = plistlib.loads(snapshot.read_bytes())
    data = json.loads(items["target"]["data"])
    assert data["unrelated"] == "preserve"
    assert data["claudeAiOauth"]["refreshToken"] == (
        "synthetic-new-refresh" if recovered else "synthetic-old-refresh"
    )
    assert ("journal" not in items) is recovered


@pytest.mark.parametrize("mode", ["pending_failed", "target_failed"])
def test_retry_known_replacement_over_transport_never_exchanges_again(runtime, mode):
    restart, snapshot, fault, exchanges = runtime
    worker = restart()
    fault.write_text(mode)
    assert not worker.renew()
    assert len(exchanges) == 1
    assert worker.renew()
    assert len(exchanges) == 1
    assert "journal" not in plistlib.loads(snapshot.read_bytes())


def test_failed_intent_persistence_prevents_exchange_over_transport(runtime):
    restart, snapshot, fault, exchanges = runtime
    worker = restart()
    fault.write_text("intent_before_save")
    assert not worker.renew()
    assert not exchanges
    assert "journal" not in plistlib.loads(snapshot.read_bytes())
