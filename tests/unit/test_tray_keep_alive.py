"""Tray lifecycle must control the same keep-alive policy as the CLI daemon."""

import sys
from unittest.mock import Mock

import pytest

from sidecar_app import daemon


@pytest.fixture
def isolated(monkeypatch):
    controller = Mock()
    runner = Mock()
    monkeypatch.setattr(daemon._sidecar, "_KEEP_ALIVE", controller)
    monkeypatch.setattr(daemon, "DaemonRunner", Mock(return_value=runner))
    return controller, runner


@pytest.mark.parametrize("flag", [None, False, True, "true", 1])
def test_start_arms_local_policy_once(isolated, flag):
    controller, runner = isolated
    instance = daemon.TrayDaemon({"keep_alive": flag})
    instance.start()
    instance.start()
    controller.arm.assert_called_once_with(
        flag is True, providers={"anthropic"} if sys.platform == "darwin" else set()
    )
    runner.start.assert_called_once()


def test_stop_disables_renewal_before_stopping_native_transport(isolated):
    controller, runner = isolated
    order = []
    controller.stop.side_effect = lambda: order.append("renewal")
    runner.stop.side_effect = lambda: order.append("transport")
    instance = daemon.TrayDaemon({})
    instance.start()
    instance.stop()
    instance.stop()
    assert order == ["renewal", "transport"]


def test_config_reload_replaces_removed_permission_and_updates_local_policy(isolated):
    controller, runner = isolated
    instance = daemon.TrayDaemon({"keep_alive": True, "claude_keychain_renewal": True})
    instance.start()
    replacement = {"keep_alive": False}
    instance.configure(replacement)
    assert runner._config is replacement
    assert instance._config is replacement
    assert "claude_keychain_renewal" not in runner._config
    assert controller.arm.call_args.args == (False,)
    assert runner.start.call_count == 1


def test_configure_stopped_daemon_never_starts_renewers(isolated):
    controller, runner = isolated
    instance = daemon.TrayDaemon({})
    instance.configure({"keep_alive": True})
    controller.arm.assert_not_called()
    runner.start.assert_not_called()


def test_start_failure_releases_controller_and_allows_later_start(isolated):
    controller, runner = isolated
    runner.start.side_effect = RuntimeError("synthetic startup failure")
    instance = daemon.TrayDaemon({"keep_alive": True})
    with pytest.raises(RuntimeError):
        instance.start()
    controller.stop.assert_called_once()
    runner.stop.assert_called_once()
    runner.start.side_effect = None
    instance.start()
    assert runner.start.call_count == 2


def test_restart_reopens_native_transport_before_arming_renewers(isolated):
    from scripts.sidecar_pkg.keychain_client import get_keychain_client

    controller, _ = isolated
    client = get_keychain_client()
    client.close()
    controller.arm.side_effect = lambda flag, **kwargs: (
        pytest.fail("transport still closed") if client._closed else None
    )
    instance = daemon.TrayDaemon({"keep_alive": True})
    instance.start()
    assert not client._closed


def test_tray_accepts_fleet_claude_activation_but_cannot_grant_keychain_permission(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(daemon, "sys", SimpleNamespace(platform="darwin"))
    from scripts.sidecar_pkg import keep_alive

    thread = Mock()
    controller = keep_alive.KeepAliveController(lambda: thread, lambda: ["anthropic", "xai"])
    monkeypatch.setattr(daemon._sidecar, "_KEEP_ALIVE", controller)
    from scripts.sidecar_pkg.source_consent import SourcePolicy

    runner = daemon._sidecar.DaemonRunner(
        SourcePolicy.enable_all({"keep_alive": False, "claude_keychain_renewal": True})
    )
    runner.start = Mock()
    runner.stop = Mock()
    monkeypatch.setattr(daemon, "DaemonRunner", Mock(return_value=runner))
    instance = daemon.TrayDaemon(runner._config)
    try:
        instance.start()
        controller.set_remote_providers({"anthropic": True, "xai": False})
        assert controller.reported_providers() == {"anthropic": True, "xai": False}
        thread.start.assert_called_once()
        assert daemon._sidecar._claude_keychain_renewal_enabled()
        instance.configure({"keep_alive": False})
        assert controller.effective_for("anthropic")
        assert not daemon._sidecar._claude_keychain_renewal_enabled()
    finally:
        instance.stop()
        keep_alive.disable()


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_tray_scope_cannot_enable_unaccepted_provider_paths(monkeypatch, platform):
    from types import SimpleNamespace

    from scripts.sidecar_pkg import keep_alive

    providers = ["anthropic", "xai", "chatgpt", "antigravity"]
    thread = Mock()
    controller = keep_alive.KeepAliveController(lambda: thread, lambda: providers)
    monkeypatch.setattr(daemon, "sys", SimpleNamespace(platform=platform), raising=False)
    monkeypatch.setattr(daemon._sidecar, "_KEEP_ALIVE", controller)
    monkeypatch.setattr(daemon, "DaemonRunner", Mock(return_value=Mock()))
    instance = daemon.TrayDaemon({"keep_alive": True})
    try:
        instance.start()
        controller.set_remote(True)
        controller.set_remote_providers(dict.fromkeys(providers, True))
        assert controller.reported_providers() == {
            name: platform == "darwin" and name == "anthropic" for name in providers
        }
        assert thread.start.call_count == (1 if platform == "darwin" else 0)
        controller.set_remote_providers({"anthropic": False, "xai": True})
        assert controller._thread is None
        assert not controller.reported()
        instance.configure({"keep_alive": False})
        assert controller._thread is None
    finally:
        instance.stop()
        keep_alive.disable()


def test_cli_controller_retains_all_provider_capabilities():
    from scripts.sidecar_pkg import keep_alive

    thread = Mock()
    providers = ["anthropic", "xai", "chatgpt", "antigravity"]
    controller = keep_alive.KeepAliveController(lambda: thread, lambda: providers)
    try:
        controller.arm(True)
        assert controller.reported_providers() == dict.fromkeys(providers, True)
        thread.start.assert_called_once()
    finally:
        controller.stop()
        keep_alive.disable()
