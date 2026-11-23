"""Synthetic Fleet commands never invoke native authorization."""

import os

import pytest

from scripts.sidecar_pkg import source_consent, source_protocol, source_settings
from scripts.sidecar_pkg.source_consent import SourcePolicy
from scripts.sidecar_pkg.source_protocol import filter_egress


def config():
    value = {
        "api_url": "https://runway.example",
        "device_api_url": "https://runway.example",
        "device_id": "a" * 32,
        "device_key": "synthetic-device-key",
    }
    return SourcePolicy.change(value, "anthropic.claude-code", read=True)


def reply(*commands):
    return {
        "protocol": 1,
        "accepted": ["anthropic.claude-code"],
        "commands": list(commands),
        "withdrawals": [],
    }


def command(number, action, generation=1):
    return {
        "id": number,
        "source_id": "anthropic.claude-code",
        "generation": generation,
        "action": action,
    }


def test_remote_pause_survives_restart_and_resume_cannot_override_off(tmp_path):
    local = config()
    source_consent.POLICY.configure(local)
    client = source_protocol.SourceClient(local, tmp_path / "state.json")
    client.receive(local, reply(command(1, "pause")))
    assert source_consent.POLICY.ticket("anthropic.claude-code") is None
    restored = source_protocol.SourceClient(local, tmp_path / "state.json")
    assert source_consent.POLICY.ticket("anthropic.claude-code") is None
    off = SourcePolicy.change(local, "anthropic.claude-code", read=False)
    source_consent.POLICY.configure(off)
    restored.receive(off, reply(command(2, "resume")))
    assert restored.state["acknowledgments"] == [{"id": 2, "status": "denied"}]
    assert source_consent.POLICY.ticket("anthropic.claude-code") is None
    if os.name != "nt":
        # The state file must not be readable by anyone but its owner.
        # Windows has no POSIX permission bits, and the client does not
        # require them there either - see scripts/sidecar_pkg/source_protocol.py,
        # which guards the same check with the same condition.
        assert (tmp_path / "state.json").stat().st_mode & 0o077 == 0


def test_setup_request_is_notification_only_and_commands_are_idempotent(tmp_path):
    local = config()
    source_consent.POLICY.configure(local)
    client = source_protocol.SourceClient(local, tmp_path / "state.json")
    client.receive(local, reply(command(1, "request_setup")))
    assert client.state["commands"]["anthropic.claude-code"]["setup_requested"] is True
    client.receive(local, reply(command(1, "request_setup")))
    assert client.state["acknowledgments"] == [{"id": 1, "status": "applied"}]
    assert source_consent.POLICY.ticket("anthropic.claude-code") is not None
    assert "synthetic-device-key" not in (tmp_path / "state.json").read_text()


def test_revoke_requires_new_local_generation_and_failed_persistence_withholds(
    tmp_path, monkeypatch
):
    local = config()
    source_consent.POLICY.configure(local)
    client = source_protocol.SourceClient(local, tmp_path / "state.json")
    client.receive(local, reply(command(1, "revoke")))
    assert source_consent.POLICY.ticket("anthropic.claude-code") is None
    client.receive(local, reply(command(2, "resume")))
    assert client.state["acknowledgments"][0]["status"] == "denied"
    newer = SourcePolicy.change(local, "anthropic.claude-code", read=True)
    source_consent.POLICY.configure(newer)
    client.apply_runtime(newer)
    assert source_consent.POLICY.ticket("anthropic.claude-code") is not None
    monkeypatch.setattr(client, "persist", lambda: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(OSError):
        client.receive(newer, reply(command(3, "pause", 2)))
    assert client.accepted == set()
    assert client.state["acknowledgments"] == [{"id": 2, "status": "denied"}]
    client.apply_runtime(newer)
    assert source_consent.POLICY.ticket("anthropic.claude-code") is None


def test_device_key_is_pinned_to_destination_and_signatures_are_domain_bound():
    local = config()
    url = local["api_url"] + "/api/v1/fleet/ingest"
    first = source_protocol.signed_headers(local, url, "legacy", "1", "ingest", b"{}")
    second = source_protocol.signed_headers(local, url, "legacy", "1", "manifest", b"{}")
    assert first["X-Runway-Device-Id"] == "a" * 32
    assert first["X-Signature"] != second["X-Signature"]
    for changed in ["https://other.example", "https://runway.example/other"]:
        assert (
            source_protocol.device_credentials(
                {**local, "api_url": changed}, changed + "/api/v1/fleet/ingest"
            )
            is None
        )
    assert (
        source_protocol.device_credentials(local, "https://runway.example.evil/api/v1/fleet/ingest")
        is None
    )


def test_egress_requires_live_consent_and_negotiated_revision(tmp_path, monkeypatch):
    local = config()
    source_consent.POLICY.configure(local)
    client = source_protocol.SourceClient(local, tmp_path / "state.json")
    monkeypatch.setattr(source_protocol, "CLIENT", client)
    ticket = source_consent.POLICY.ticket("anthropic.claude-code")
    card = {
        "metadata": {
            "provider_id": "anthropic",
            "credential_origin": "keychain:Claude Code-credentials",
            "oauth_token": "synthetic-access",
            "refresh_token": "synthetic-local-only",
            "keychain_consent": source_consent.stamp(ticket),
        }
    }
    assert filter_egress({"metrics": [card]}, config=local)["metrics"] == []
    client.receive(local, reply())
    forwarded = filter_egress({"metrics": [card]}, config=local)["metrics"][0]["metadata"]
    assert set(forwarded["keychain_consent"]) == {"source_id", "revision", "generation"}
    assert "refresh_token" not in forwarded
    assert card["metadata"]["refresh_token"] == "synthetic-local-only"
    source_consent.POLICY.configure(SourcePolicy.change(local, "anthropic.claude-code", read=False))
    assert filter_egress({"metrics": [card]}, config=local)["metrics"] == []


def test_current_controls_protect_fresh_process_after_another_process_acknowledged(tmp_path):
    local = config()
    source_consent.POLICY.configure(local)
    fresh = source_protocol.SourceClient(local, tmp_path / "fresh.json")
    response = reply()
    response["controls"] = [
        {"source_id": "anthropic.claude-code", "paused": True, "revoked_through": -1}
    ]
    fresh.receive(local, response)
    assert source_consent.POLICY.ticket("anthropic.claude-code") is None
    restarted = source_protocol.SourceClient(local, tmp_path / "fresh.json")
    assert source_consent.POLICY.ticket("anthropic.claude-code") is None


def test_non_macos_reports_no_new_scrapers_but_can_withdraw_old_consent(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    local = config()
    client = source_protocol.SourceClient(local, tmp_path / "state.json")
    assert client.payload(local, source_settings.report(local))["sources"] == []
    off = SourcePolicy.change(local, "anthropic.claude-code", read=False)
    off["keychain_withdrawals"] = {"anthropic.claude-code": 2}
    sources = client.payload(off, source_settings.report(off))["sources"]
    assert len(sources) == 1 and sources[0]["read"] is False and sources[0]["new"] is False


def test_claude_refresh_cannot_leave_when_provider_is_only_on_the_card():
    card = {
        "provider_id": "anthropic",
        "metadata": {"oauth_token": "synthetic-access", "refresh_token": "synthetic-local-refresh"},
    }
    forwarded = filter_egress({"metrics": [card]})["metrics"][0]["metadata"]
    assert "refresh_token" not in forwarded
    assert forwarded["oauth_read_only"] == "1"
    assert card["metadata"]["refresh_token"] == "synthetic-local-refresh"


def test_shared_native_denial_is_reported_without_withdrawing_local_intent(monkeypatch):
    from scripts.sidecar_pkg import keychain_client, source_settings

    local = config()
    for source_id in ("anthropic.chrome", "chatgpt.chrome"):
        local = SourcePolicy.change(local, source_id, read=True)
    client = keychain_client.KeychainClient(command=["synthetic-never-started"])
    client.block_access("storage:Chrome Safe Storage", "denied")
    monkeypatch.setattr(keychain_client, "_CLIENT", client)
    rows = {row["id"]: row for row in source_settings.report(local)["sources"]}
    for source_id in ("anthropic.chrome", "chatgpt.chrome"):
        assert rows[source_id]["read"] is True
        assert rows[source_id]["state"] == "Action required"
    assert rows["kimi_coding.chrome"]["read"] is False
    assert rows["kimi_coding.chrome"]["state"] == "Off"
    assert client._process is None


def test_device_auth_failure_surfaces_repair_without_shared_key_downgrade(
    tmp_path, monkeypatch, caplog
):
    from urllib.error import HTTPError

    from scripts.sidecar_pkg import source_settings, transport

    local = config()
    source_consent.POLICY.configure(local)
    client = source_protocol.SourceClient(local, tmp_path / "state.json")
    monkeypatch.setattr(source_protocol, "CLIENT", client)
    requests = []

    def reject(req, **kwargs):
        requests.append(req)
        raise HTTPError(req.full_url, 401, "synthetic-secret-not-for-logs", {}, None)

    monkeypatch.setattr(transport, "urlopen", reject)
    assert not client.sync(local, source_settings.report)
    assert not client.sync(local, source_settings.report)
    assert source_settings.report(local)["re_pair_required"] is True
    assert all(req.get_header("X-runway-device-id") == local["device_id"] for req in requests)
    assert caplog.text.count("re-pair this sidecar") == 1
    assert "synthetic-secret" not in caplog.text
    client.receive(local, reply())
    assert source_settings.report(local)["re_pair_required"] is False
    moved = {**local, "api_url": "https://other.example"}
    assert source_settings.report(moved)["re_pair_required"] is True


def test_nonmac_cookie_resume_does_not_require_or_grant_keychain_consent(tmp_path, monkeypatch):
    monkeypatch.setattr(source_protocol.sys, "platform", "linux")
    local = config()
    source_consent.POLICY.configure(local)
    client = source_protocol.SourceClient(local, tmp_path / "state.json")
    pause = {**command(1, "pause", 0), "source_id": "anthropic.chrome"}
    resume = {**command(2, "resume", 0), "source_id": "anthropic.chrome"}
    client.receive(local, reply(pause))
    client.receive(local, reply(resume))
    assert client.state["acknowledgments"] == [{"id": 2, "status": "applied"}]
    assert source_consent.POLICY.ticket("anthropic.chrome") is None
    revoke = {**command(3, "revoke", 0), "source_id": "anthropic.chrome"}
    client.receive(local, reply(revoke))
    client.receive(local, reply({**resume, "id": 4}))
    assert client.state["acknowledgments"] == [{"id": 4, "status": "denied"}]


def test_windows_controls_acknowledge_resume_without_granting_mac_consent(tmp_path, monkeypatch):
    monkeypatch.setattr(source_protocol.sys, "platform", "win32")
    local = config()
    local.pop("keychain_sources")
    source_consent.POLICY.configure(local)
    client = source_protocol.SourceClient(local, tmp_path / "state.json")
    windows = {"source_id": "github.windows-credential", "generation": 0}
    client.receive(local, reply({**command(1, "pause"), **windows}))
    restored = source_protocol.SourceClient(local, tmp_path / "state.json")
    restored.receive(local, reply({**command(2, "resume"), **windows}))
    assert restored.state["acknowledgments"] == [{"id": 2, "status": "applied"}]
    restored.receive(local, reply({**command(3, "revoke"), **windows}))
    restored.receive(local, reply({**command(4, "resume"), **windows}))
    assert restored.state["acknowledgments"] == [{"id": 4, "status": "denied"}]
    assert all(source_consent.POLICY.ticket(key) is None for key in source_consent.catalog())
    assert "keychain_sources" not in local


def test_sync_failure_reasons_report_fields_and_log_once_per_change(tmp_path, monkeypatch, caplog):
    import json
    import logging
    from urllib.error import HTTPError, URLError

    from scripts.sidecar_pkg import source_settings, transport

    local = config()
    source_consent.POLICY.configure(local)
    client = source_protocol.SourceClient(local, tmp_path / "state.json")
    monkeypatch.setattr(source_protocol, "CLIENT", client)

    # 1. Before any sync has completed.
    with caplog.at_level(logging.WARNING, logger="scripts.sidecar_pkg.source_protocol"):
        rep = source_settings.report(local)
    assert rep["forwarding_supported"] is False
    assert rep["re_pair_required"] is False
    assert rep["sync_state"] == "not_synced_yet"
    assert rep["sync_reason"] is None
    assert caplog.records == []

    class _FakeResponse:
        def __init__(self, url: str, body: bytes):
            self._url = url
            self._body = body

        def geturl(self) -> str:
            return self._url

        def read(self, _limit: int = -1) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    # 2. Redirect (response.geturl() != url).
    caplog.clear()
    monkeypatch.setattr(
        transport,
        "urlopen",
        lambda _req, **_kw: _FakeResponse("https://evil.example/secret-token-redirect", b"{}"),
    )
    assert not client.sync(local, source_settings.report)
    assert not client.sync(local, source_settings.report)
    rep = source_settings.report(local)
    assert rep["sync_state"] == "sync_failed"
    assert rep["sync_reason"] == "redirect"
    assert rep["re_pair_required"] is False
    assert len(caplog.records) == 1
    assert "redirect" in caplog.text
    assert "secret-token-redirect" not in caplog.text

    # 3. 404 -> server_unsupported.
    caplog.clear()
    monkeypatch.setattr(
        transport,
        "urlopen",
        lambda req, **_kw: (_ for _ in ()).throw(
            HTTPError(req.full_url, 404, "secret-404-body", {}, None)
        ),
    )
    assert not client.sync(local, source_settings.report)
    assert not client.sync(local, source_settings.report)
    rep = source_settings.report(local)
    assert rep["sync_state"] == "server_unsupported"
    assert rep["sync_reason"] == "http_404"
    assert rep["re_pair_required"] is False
    assert len(caplog.records) == 1
    assert "http_404" in caplog.text
    assert "404" in caplog.text
    assert "secret-404-body" not in caplog.text

    # 4. 500 -> sync_failed (http_500).
    caplog.clear()
    monkeypatch.setattr(
        transport,
        "urlopen",
        lambda req, **_kw: (_ for _ in ()).throw(
            HTTPError(req.full_url, 500, "secret-500-body", {}, None)
        ),
    )
    assert not client.sync(local, source_settings.report)
    assert not client.sync(local, source_settings.report)
    rep = source_settings.report(local)
    assert rep["sync_state"] == "sync_failed"
    assert rep["sync_reason"] == "http_500"
    assert rep["re_pair_required"] is False
    assert len(caplog.records) == 1
    assert "http_500" in caplog.text
    assert "500" in caplog.text
    assert "secret-500-body" not in caplog.text

    # 5. Network error (URLError).
    caplog.clear()
    monkeypatch.setattr(
        transport,
        "urlopen",
        lambda _req, **_kw: (_ for _ in ()).throw(URLError("secret-network-url")),
    )
    assert not client.sync(local, source_settings.report)
    rep = source_settings.report(local)
    assert rep["sync_state"] == "sync_failed"
    assert rep["sync_reason"] == "network"
    assert len(caplog.records) == 1
    assert "network" in caplog.text
    assert "URLError" in caplog.text
    assert "secret-network-url" not in caplog.text

    # 6. Invalid JSON -> invalid_reply.
    caplog.clear()
    monkeypatch.setattr(
        transport,
        "urlopen",
        lambda req, **_kw: _FakeResponse(req.full_url, b"{not-json-secret-body"),
    )
    assert not client.sync(local, source_settings.report)
    rep = source_settings.report(local)
    assert rep["sync_state"] == "sync_failed"
    assert rep["sync_reason"] == "invalid_reply"
    assert len(caplog.records) == 1
    assert "invalid_reply" in caplog.text
    assert "JSONDecodeError" in caplog.text
    assert "secret-body" not in caplog.text

    # 7. Reply without protocol: 1 -> server_unsupported.
    caplog.clear()
    monkeypatch.setattr(
        transport,
        "urlopen",
        lambda req, **_kw: _FakeResponse(
            req.full_url, json.dumps({"commands": [], "accepted": []}).encode()
        ),
    )
    assert not client.sync(local, source_settings.report)
    rep = source_settings.report(local)
    assert rep["sync_state"] == "server_unsupported"
    assert rep["sync_reason"] == "unsupported_protocol"
    assert len(caplog.records) == 1
    assert "unsupported_protocol" in caplog.text

    # 8. persist() raising OSError -> local_state.
    caplog.clear()
    monkeypatch.setattr(
        transport,
        "urlopen",
        lambda req, **_kw: _FakeResponse(req.full_url, json.dumps(reply()).encode()),
    )
    real_persist = client.persist
    monkeypatch.setattr(client, "persist", lambda: (_ for _ in ()).throw(OSError("disk full")))
    assert not client.sync(local, source_settings.report)
    rep = source_settings.report(local)
    assert rep["sync_state"] == "sync_failed"
    assert rep["sync_reason"] == "local_state"
    assert len(caplog.records) == 1
    assert "local_state" in caplog.text
    assert "OSError" in caplog.text

    # 9. Recovery clears the logged-reason guard so a subsequent failure logs again.
    monkeypatch.setattr(client, "persist", real_persist)
    assert client.sync(local, source_settings.report)
    rep = source_settings.report(local)
    assert rep["sync_state"] == "ok"
    assert rep["sync_reason"] is None
    assert rep["forwarding_supported"] is True

    caplog.clear()
    monkeypatch.setattr(
        transport,
        "urlopen",
        lambda req, **_kw: (_ for _ in ()).throw(HTTPError(req.full_url, 500, "err", {}, None)),
    )
    assert not client.sync(local, source_settings.report)
    assert len(caplog.records) == 1
    assert "http_500" in caplog.text


def test_unpaired_report_reports_not_paired(monkeypatch):
    monkeypatch.setattr(source_protocol, "CLIENT", None)
    unpaired = source_settings.report({"api_url": "https://runway.example"})
    assert unpaired["sync_state"] == "not_paired"
    assert unpaired["sync_reason"] is None
    assert unpaired["re_pair_required"] is False
    assert unpaired["forwarding_supported"] is False

    paired_before_sync = source_settings.report(config())
    assert paired_before_sync["sync_state"] == "not_synced_yet"
    assert paired_before_sync["sync_reason"] is None


def test_synced_report_holds_sync_lock_against_concurrent_sync(tmp_path, monkeypatch):
    import json
    import threading

    from scripts.sidecar_pkg import transport

    local = config()
    source_consent.POLICY.configure(local)
    state_path = tmp_path / "state.json"

    class _FakeResponse:
        def __init__(self, url: str):
            self._url = url

        def geturl(self) -> str:
            return self._url

        def read(self, _limit: int = -1) -> bytes:
            return json.dumps(reply()).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(transport, "urlopen", lambda req, **_kw: _FakeResponse(req.full_url))
    monkeypatch.setattr(source_protocol, "CLIENT", None)

    assert source_protocol.sync(local, state_path, source_settings.report)

    entered_report = threading.Event()
    release_report = threading.Event()
    sync_started = threading.Event()
    sync_done = threading.Event()

    def slow_report(cfg):
        entered_report.set()
        release_report.wait(timeout=5)
        return source_settings.report(cfg)

    report_holder = []
    reader = threading.Thread(
        target=lambda: report_holder.append(
            source_protocol.synced_report(local, state_path, slow_report)
        )
    )
    reader.start()
    assert entered_report.wait(timeout=5)

    def run_bg_sync():
        sync_started.set()
        source_protocol.sync(local, state_path, source_settings.report)
        sync_done.set()

    writer = threading.Thread(target=run_bg_sync)
    writer.start()
    assert sync_started.wait(timeout=5)
    # Give the background sync thread a moment to block on _SYNC_LOCK; it must not clear
    # CLIENT.accepted before slow_report finishes.
    assert not sync_done.wait(timeout=0.05)

    release_report.set()
    reader.join(timeout=5)
    writer.join(timeout=5)
    assert sync_done.is_set()
    assert report_holder[0]["sync_state"] == "ok"
    assert report_holder[0]["forwarding_supported"] is True


def test_daemon_runner_syncs_before_first_collection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts import sidecar
    from scripts.sidecar_pkg import transport

    order = []
    local = config()
    local["api_key"] = "synthetic-api-key"  # pragma: allowlist secret
    local["poll_interval"] = 60
    monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: tmp_path)
    monkeypatch.setattr(
        source_protocol,
        "sync",
        lambda *_args, **_kwargs: order.append("sync") or True,
    )
    monkeypatch.setattr(sidecar, "scope_login_dirs", lambda _cfg: None)

    runner = sidecar.DaemonRunner(local)

    def fake_collection(_cfg, providers=None):
        order.append("collect")
        runner.stop()
        return SimpleNamespace(
            metrics=[],
            events=[],
            session_names=[],
            error_count=0,
            completed_providers=[],
        )

    monkeypatch.setattr(sidecar, "run_collection", fake_collection)
    monkeypatch.setattr(
        transport, "urlopen", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("stop"))
    )

    runner._loop()
    assert order == ["sync", "collect"]
