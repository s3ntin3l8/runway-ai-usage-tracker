"""Synthetic Fleet commands never invoke native authorization."""

import pytest

from scripts.sidecar_pkg import source_consent, source_protocol
from scripts.sidecar_pkg.source_consent import SourcePolicy


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
    assert source_consent.filter_egress({"metrics": [card]}, config=local)["metrics"] == []
    client.receive(local, reply())
    forwarded = source_consent.filter_egress({"metrics": [card]}, config=local)["metrics"][0][
        "metadata"
    ]
    assert set(forwarded["keychain_consent"]) == {"source_id", "revision", "generation"}
    assert "refresh_token" not in forwarded
    assert card["metadata"]["refresh_token"] == "synthetic-local-only"
    source_consent.POLICY.configure(SourcePolicy.change(local, "anthropic.claude-code", read=False))
    assert source_consent.filter_egress({"metrics": [card]}, config=local)["metrics"] == []


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
    assert client.payload(local)["sources"] == []
    off = SourcePolicy.change(local, "anthropic.claude-code", read=False)
    off["keychain_withdrawals"] = {"anthropic.claude-code": 2}
    sources = client.payload(off)["sources"]
    assert len(sources) == 1 and sources[0]["read"] is False and sources[0]["new"] is False


def test_claude_refresh_cannot_leave_when_provider_is_only_on_the_card():
    card = {
        "provider_id": "anthropic",
        "metadata": {"oauth_token": "synthetic-access", "refresh_token": "synthetic-local-refresh"},
    }
    forwarded = source_consent.filter_egress({"metrics": [card]})["metrics"][0]["metadata"]
    assert "refresh_token" not in forwarded
    assert forwarded["oauth_read_only"] == "1"
    assert card["metadata"]["refresh_token"] == "synthetic-local-refresh"
