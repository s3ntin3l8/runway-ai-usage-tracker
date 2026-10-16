"""Background tray refreshes are queued, with AppKit rendering on the main loop."""

import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from sidecar_app import tray


class RecordingIcon:
    """Record native icon operations without opening a real system tray."""

    def __init__(self):
        self.operations = []
        self.refreshed = threading.Event()
        self._title = "before refresh"

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self._title = value
        self.operations.append(("title", threading.get_ident()))

    @property
    def icon(self):
        return None

    @icon.setter
    def icon(self, value):
        self.operations.append(("image", threading.get_ident()))

    def update_menu(self):
        self.operations.append(("menu", threading.get_ident()))
        self.refreshed.set()


@pytest.fixture
def instance(monkeypatch, tmp_path):
    monkeypatch.setattr(tray.SidecarTray, "_refresh_rollback", lambda self: None)
    return tray.SidecarTray(
        SimpleNamespace(status="ok", stats_summary=""), {}, tmp_path / "config.yaml"
    )


@pytest.mark.parametrize("available", [True, False])
def test_update_availability_waits_for_the_refresh_queue(instance, available):
    icon = RecordingIcon()
    instance._icon = icon
    instance._update_available = not available

    instance.set_update_available(available)

    assert instance._update_available is available
    assert icon.operations == []
    assert instance._update_queue.get_nowait() == "ok"


def test_windows_refreshes_render_on_the_single_drain_worker(instance, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    icon = RecordingIcon()
    instance._icon = icon
    instance._update_icon("warn")
    instance.set_update_available(True)
    worker = threading.Thread(target=instance._drain_updates)
    worker.start()
    try:
        assert icon.refreshed.wait(2)
        assert "update available" in icon.title
        assert {thread_id for _, thread_id in icon.operations} == {worker.ident}
    finally:
        instance._icon = None
        worker.join(2)
        assert not worker.is_alive()


def test_macos_refreshes_wait_for_main_loop_dispatch(instance, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "darwin")
    scheduled = queue.Queue()
    dispatched = threading.Event()

    def call_after(callback, *args):
        scheduled.put((callback, args))
        dispatched.set()

    monkeypatch.setattr(tray, "_call_after", call_after, raising=False)
    icon = RecordingIcon()
    instance._icon = icon
    instance.set_update_available(True)
    worker = threading.Thread(target=instance._drain_updates)
    worker.start()
    try:
        assert dispatched.wait(2), "the worker must schedule an AppKit main-loop refresh"
        assert icon.operations == []
        callback, args = scheduled.get_nowait()
        callback(*args)
        assert "update available" in icon.title
        assert {thread_id for _, thread_id in icon.operations} == {threading.get_ident()}
        icon.operations.clear()
        instance._icon = None
        callback(*args)
        assert icon.operations == []
    finally:
        instance._icon = None
        worker.join(2)
        assert not worker.is_alive()


def test_render_failure_is_logged_and_the_next_refresh_succeeds(instance, monkeypatch, caplog):
    icon = RecordingIcon()
    instance._icon = icon
    build_icon = tray._build_status_icon
    attempts = []

    def fail_once(status):
        attempts.append(status)
        if len(attempts) == 1:
            raise OSError("unreadable logo")
        return build_icon(status)

    monkeypatch.setattr(tray, "_build_status_icon", fail_once)
    instance._apply_status_update("warn")
    assert not icon.refreshed.is_set()
    assert "Could not refresh tray" in caplog.text
    instance._apply_status_update("ok")
    assert icon.refreshed.is_set()
    assert icon.title == "Runway Sidecar — Healthy\n"


def test_dispatch_failure_does_not_stop_the_drain_worker(instance, monkeypatch, caplog):
    monkeypatch.setattr(tray.sys, "platform", "darwin")
    dispatched = threading.Event()
    attempts = []

    def fail_once(callback, *args):
        attempts.append(args)
        if len(attempts) == 1:
            raise RuntimeError("dispatch unavailable")
        dispatched.set()

    monkeypatch.setattr(tray, "_call_after", fail_once, raising=False)
    instance._icon = RecordingIcon()
    instance._update_icon("warn")
    instance._update_icon("ok")
    worker = threading.Thread(target=instance._drain_updates)
    worker.start()
    try:
        assert dispatched.wait(2), "a failed dispatch must not kill the refresh worker"
        assert "Could not dispatch tray refresh" in caplog.text
    finally:
        instance._icon = None
        worker.join(2)
        assert not worker.is_alive()


@pytest.mark.skipif(sys.platform != "darwin", reason="Exercises the real AppKit main loop")
def test_native_macos_dispatch_renders_on_the_main_thread(instance):
    foundation = pytest.importorskip("Foundation")
    pytest.importorskip("PyObjCTools.AppHelper")
    icon = RecordingIcon()
    instance._icon = icon
    instance.set_update_available(True)
    worker = threading.Thread(target=instance._drain_updates)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        loop = foundation.NSRunLoop.currentRunLoop()
        while not icon.refreshed.is_set() and time.monotonic() < deadline:
            loop.runUntilDate_(foundation.NSDate.dateWithTimeIntervalSinceNow_(0.01))
        assert icon.refreshed.is_set(), "AppKit must deliver the queued refresh"
        assert "update available" in icon.title
        assert {thread_id for _, thread_id in icon.operations} == {threading.main_thread().ident}
    finally:
        instance._icon = None
        worker.join(2)
        assert not worker.is_alive()
