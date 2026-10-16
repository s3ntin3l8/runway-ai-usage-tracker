"""The manual tray check uses the selected channel and reports its result."""

from unittest.mock import Mock

import pytest

from sidecar_app import __version__, tray


@pytest.mark.parametrize(
    ("available", "expected_flag", "expected_notice"),
    [
        ("edge build abc123", True, "Update available: edge build abc123"),
        (None, False, "Runway Sidecar is up to date."),
    ],
)
def test_manual_check_updates_menu_state_and_notifies(
    monkeypatch, available, expected_flag, expected_notice
):
    instance = tray.SidecarTray.__new__(tray.SidecarTray)
    instance.set_update_available = Mock()
    icon = Mock()
    check = Mock(return_value=available)
    monkeypatch.setattr("scripts.sidecar_pkg.update_check.check_once", check)
    monkeypatch.setattr("sidecar_app.updater._resolve_channel", lambda: "edge")

    instance._check_updates_now(icon)

    check.assert_called_once_with(__version__, "edge", strict=True)
    instance.set_update_available.assert_called_once_with(expected_flag)
    icon.notify.assert_called_once_with(expected_notice, "Runway Sidecar")


def test_failed_manual_check_does_not_change_menu_state(monkeypatch):
    instance = tray.SidecarTray.__new__(tray.SidecarTray)
    instance.set_update_available = Mock()
    icon = Mock()
    monkeypatch.setattr(
        "scripts.sidecar_pkg.update_check.check_once", Mock(side_effect=OSError("offline"))
    )
    monkeypatch.setattr("sidecar_app.updater._resolve_channel", lambda: "edge")

    instance._check_updates_now(icon)

    instance.set_update_available.assert_not_called()
    icon.notify.assert_called_once_with(
        "Could not check for updates. Try again or view releases.", "Runway Sidecar"
    )


def test_failed_channel_resolution_notifies_instead_of_crashing(monkeypatch):
    instance = tray.SidecarTray.__new__(tray.SidecarTray)
    instance.set_update_available = Mock()
    icon = Mock()
    check = Mock()
    monkeypatch.setattr("scripts.sidecar_pkg.update_check.check_once", check)
    monkeypatch.setattr(
        "sidecar_app.updater._resolve_channel", Mock(side_effect=OSError("bad config"))
    )

    instance._check_updates_now(icon)

    check.assert_not_called()
    instance.set_update_available.assert_not_called()
    icon.notify.assert_called_once_with(
        "Could not check for updates. Try again or view releases.", "Runway Sidecar"
    )


def test_menu_check_dispatches_checker_and_keeps_releases_separate(monkeypatch):
    instance = tray.SidecarTray.__new__(tray.SidecarTray)
    instance._daemon = Mock(status="ok", stats_summary="")
    instance._config = {}
    instance._paused = False
    instance._update_available = False
    instance._manual_check_lock = tray.threading.Lock()
    instance._rollback_version = None
    instance._settings_server = None
    instance._on_reload_config = None
    instance._check_updates_now = Mock()
    worker = Mock()
    thread_factory = Mock(return_value=worker)
    open_browser = Mock()
    monkeypatch.setattr(tray.threading, "Thread", thread_factory)
    monkeypatch.setattr(tray.webbrowser, "open", open_browser)
    icon = Mock()

    items = {item.text: item for item in instance._build_menu().items if isinstance(item.text, str)}
    items["Check for Updates…"]._action(icon, items["Check for Updates…"])
    items["Check for Updates…"]._action(icon, items["Check for Updates…"])
    thread_factory.assert_called_once()
    assert thread_factory.call_args.kwargs["name"] == "runway-manual-update-check"
    assert thread_factory.call_args.kwargs["daemon"] is True
    worker.start.assert_called_once_with()
    open_browser.assert_not_called()
    icon.notify.assert_called_once_with("An update check is already running.", "Runway Sidecar")
    thread_factory.call_args.kwargs["target"]()
    instance._check_updates_now.assert_called_once_with(icon)
    assert not instance._manual_check_lock.locked()

    items["Check for Updates…"]._action(icon, items["Check for Updates…"])
    assert thread_factory.call_count == 2

    items["View Releases…"]._action(icon, items["View Releases…"])
    open_browser.assert_called_once_with(tray.RELEASES_URL)
