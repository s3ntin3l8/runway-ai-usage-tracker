"""Update-invalidated Keychain re-authorization detection, notice, and one-click recovery."""

import json
from unittest.mock import Mock

import pytest

from app.models.source_controls import SourceSyncRequest
from scripts.sidecar_pkg import keychain_client, source_consent, source_settings, source_setup
from scripts.sidecar_pkg.source_consent import REAUTH_REQUIRED_STATE, SourcePolicy
from scripts.sidecar_pkg.source_protocol import SourceClient
from sidecar_app import tray


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = SourcePolicy.change(
        {"api_url": "https://synthetic.example"}, "anthropic.claude-code", read=True
    )
    config = SourcePolicy.change(config, "anthropic.chrome", read=True)
    state_path = tmp_path / "source-reads.json"
    identity = ["cdhash-v1"]
    monkeypatch.setattr(source_consent, "current_code_identity", lambda: identity[0])
    policy = SourcePolicy(config)
    policy.configure_state(state_path)
    monkeypatch.setattr(source_consent, "POLICY", policy)
    monkeypatch.setattr(keychain_client, "_CLIENT", None)
    monkeypatch.setattr(source_settings, "_safari_report", lambda _: {})
    return config, state_path, identity


def _authorize(source_ids: list[str], identity_fn=None) -> source_setup.SetupController:
    controller = source_setup.SetupController(
        launch=lambda _source_id: True,
        verify=lambda source_id, ticket: source_consent.POLICY.record_read(ticket) or True,
        code_identity=identity_fn,
    )
    controller.start_all(source_ids)
    controller.wait(2)
    return controller


def test_same_identity_gives_no_reauthorization_notice(env, tmp_path):
    config, state_path, _identity = env
    _authorize(["anthropic.claude-code"])

    restarted = SourcePolicy(config)
    restarted.configure_state(state_path)
    source_consent.POLICY = restarted

    report = source_settings.report(config)
    row = next(r for r in report["sources"] if r["id"] == "anthropic.claude-code")
    assert row["state"].startswith("Authorized (checked ")
    assert row["state_reason"] is None
    assert report["reauthorization_count"] == 0
    assert restarted.reauthorization_notice() is None


def test_changed_identity_marks_sources_reports_count_and_notifies_once_per_change(
    env, tmp_path, monkeypatch
):
    config, state_path, identity = env
    _authorize(["anthropic.claude-code", "anthropic.chrome"])

    saved_text = state_path.read_text(encoding="utf-8")
    saved = json.loads(saved_text)
    assert saved["anthropic.claude-code"]["code_identity"] == "cdhash-v1"
    assert not any(
        word in saved_text
        for word in ("secret", "token", "cookie", "account", "synthetic.example", "generation")
    )

    # Simulate app update: new cdhash, fresh process state, and even a blocked read/group.
    identity[0] = "cdhash-v2"
    restarted = SourcePolicy(config)
    restarted.configure_state(state_path)
    restarted.record_block(restarted.ticket("anthropic.chrome"), "denied")
    monkeypatch.setattr(source_consent, "POLICY", restarted)
    monkeypatch.setattr(
        keychain_client,
        "access_groups_blocked",
        lambda groups: "claude-code" in groups,
    )

    monkeypatch.setattr(source_settings.sys, "platform", "darwin")
    report = source_settings.report(config)
    by_id = {r["id"]: r for r in report["sources"]}
    assert by_id["anthropic.claude-code"]["state"] == REAUTH_REQUIRED_STATE
    assert by_id["anthropic.claude-code"]["state_reason"] == "app_updated"
    assert by_id["anthropic.chrome"]["state"] == REAUTH_REQUIRED_STATE
    assert by_id["anthropic.chrome"]["state_reason"] == "app_updated"
    assert report["reauthorization_count"] == 2

    # Source report sent to Fleet carries state and state_reason.
    payload = SourceClient(config, tmp_path / "control.json").payload(config, report)
    SourceSyncRequest.model_validate(payload)
    fleet_by_id = {r["source_id"]: r for r in payload["sources"]}
    assert fleet_by_id["anthropic.claude-code"]["state"] == REAUTH_REQUIRED_STATE
    assert fleet_by_id["anthropic.claude-code"]["state_reason"] == "app_updated"

    # Tray notification fires once for this identity change, not on repeat checks/restarts.
    monkeypatch.setattr(tray.sys, "platform", "darwin")
    instance = tray.SidecarTray.__new__(tray.SidecarTray)
    instance._icon = Mock()
    assert instance.notify_reauthorization() is True
    instance._icon.notify.assert_called_once()
    msg, title = instance._icon.notify.call_args.args
    assert "2 sources need re-authorization after the update" in msg
    assert "Re-authorization Needed" in title

    instance._icon.notify.reset_mock()
    assert instance.notify_reauthorization() is False
    instance._icon.notify.assert_not_called()

    # A second restart on the same cdhash-v2 also does not re-notify.
    restarted_again = SourcePolicy(config)
    restarted_again.configure_state(state_path)
    monkeypatch.setattr(source_consent, "POLICY", restarted_again)
    assert instance.notify_reauthorization() is False
    instance._icon.notify.assert_not_called()


def test_reauthorizing_records_new_identity_and_clears_notice(env, monkeypatch):
    config, state_path, identity = env
    _authorize(["anthropic.claude-code"])

    # Update changes cdhash and restarts the sidecar.
    identity[0] = "cdhash-v2"
    restarted = SourcePolicy(config)
    restarted.configure_state(state_path)
    monkeypatch.setattr(source_consent, "POLICY", restarted)
    assert restarted.needs_reauthorization("anthropic.claude-code") is True
    assert restarted.reauthorization_count() == 1

    # One-click Authorize N enabled re-authorizes and records the new identity.
    controller = _authorize(["anthropic.claude-code"])

    assert controller.state("anthropic.claude-code") == "Limited"
    assert restarted.needs_reauthorization("anthropic.claude-code") is False
    assert restarted.reauthorization_count() == 0
    assert restarted.reauthorization_notice() is None

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["anthropic.claude-code"]["code_identity"] == "cdhash-v2"
    assert "notified_code_identity" not in saved["anthropic.claude-code"]

    # A subsequent update notifies once again.
    identity[0] = "cdhash-v3"
    restarted = SourcePolicy(config)
    restarted.configure_state(state_path)
    monkeypatch.setattr(source_consent, "POLICY", restarted)
    notice = restarted.reauthorization_notice()
    assert notice is not None
    assert "1 source needs re-authorization after the update" in notice


def test_current_code_identity_returns_none_off_darwin(monkeypatch):
    monkeypatch.setattr(source_consent.sys, "platform", "linux")
    assert source_consent.current_code_identity() is None
