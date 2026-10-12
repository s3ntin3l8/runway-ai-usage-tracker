"""Windows native regressions: exclusive loopback ownership and a real swap helper."""

import ctypes
import ctypes.wintypes
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from scripts import sidecar
from scripts.sidecar_pkg import self_update
from sidecar_app.settings_server import SettingsServer


def make_settings(port: int, owner: str) -> SettingsServer:
    return SettingsServer(
        lambda: {},
        lambda: {"owner": owner},
        lambda config: None,
        lambda: None,
        lambda: None,
        lambda: None,
        port=port,
    )


def test_settings_servers_have_distinct_ports_and_responses():
    first = make_settings(0, "first")
    second = None
    try:
        first_port = first.start()
        second = make_settings(first_port, "second")
        second_port = second.start()
        assert first_port != second_port
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for port, owner in ((first_port, "first"), (second_port, "second")):
            with opener.open(f"http://127.0.0.1:{port}/status", timeout=2) as response:
                assert json.load(response)["owner"] == owner
    finally:
        if second is not None:
            second.stop()
        first.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows socket exclusivity")
def test_reuse_socket_cannot_share_settings_port():
    settings = make_settings(0, "owner")
    try:
        port = settings.start()
        with socket.socket() as contender:
            contender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                contender.bind(("127.0.0.1", port))
    finally:
        settings.stop()


@pytest.mark.skipif(os.name != "nt", reason="Executes the actual Windows batch helper")
def test_native_swap_from_directory_with_spaces(tmp_path):
    install_dir = tmp_path / "Runway Sidecar"
    install_dir.mkdir()
    installed = install_dir / "RunwaySidecar.exe"
    incoming = install_dir / "RunwaySidecar.new.exe"
    installed.write_bytes(b"original")
    incoming.write_bytes(b"replacement")
    command = (
        "import pathlib,sys; from scripts.sidecar_pkg.self_update import _apply_windows; "
        "sys.exit(0 if _apply_windows(pathlib.Path(sys.argv[1]), "
        "pathlib.Path(sys.argv[2]), restart=False) else 1)"
    )
    result = subprocess.run(
        [sys.executable, "-c", command, str(installed), str(incoming)],
        cwd=pathlib.Path(__file__).resolve().parents[2],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and installed.read_bytes() != b"replacement":
        time.sleep(0.1)
    assert installed.read_bytes() == b"replacement"
    assert installed.with_name(installed.name + ".previous").read_bytes() == b"original"


def test_early_lock_release_closes_handle_and_allows_immediate_reacquisition(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "_sidecar_dir", lambda: tmp_path)
    with self_update._single_flight() as acquired:
        assert acquired
        lease = self_update._ACTIVE_LOCK_LEASE
        assert lease is not None
        self_update._release_lock()
        with pytest.raises(OSError):
            os.fstat(lease[0])
        assert not (tmp_path / self_update._LOCK_NAME).exists()
        # Exit the older context while a newer context owns the lock. It must
        # not close a recycled descriptor or delete the newer owner's file.
        newer = self_update._single_flight()
        assert newer.__enter__()
    assert (tmp_path / self_update._LOCK_NAME).exists()
    newer.__exit__(None, None, None)
    assert not (tmp_path / self_update._LOCK_NAME).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows lock cleanup before hard process exit")
def test_native_update_lock_is_gone_after_hard_exit(tmp_path):
    command = (
        "import os; from scripts.sidecar_pkg.self_update import _single_flight,_release_lock; "
        "context=_single_flight(); assert context.__enter__(); _release_lock(); os._exit(0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=pathlib.Path(__file__).resolve().parents[2],
        env={**os.environ, "RUNWAY_CONFIG_DIR": str(tmp_path)},
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert not (tmp_path / "sidecar/self-update.lock").exists()


def test_release_does_not_remove_another_process_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "_sidecar_dir", lambda: tmp_path)
    lock = tmp_path / self_update._LOCK_NAME
    lock.write_text("another updater")
    self_update._release_lock()
    assert lock.read_text() == "another updater"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows file-sharing refusal")
def test_locked_queue_replacement_retains_original_and_retry_closes_handles(tmp_path, monkeypatch):
    queue_file = tmp_path / "fixture.jsonl"
    queue_file.write_text(
        "".join(
            json.dumps({"payload": {"events": [{"event_id": str(i)}]}}) + "\n" for i in range(3)
        )
    )
    original = queue_file.read_bytes()
    monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path)
    monkeypatch.setattr(sidecar, "ensure_dirs", lambda: None)
    replies = iter([(True, {}, 200), (False, {}, 503), (False, {}, 503)])
    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", lambda *a, **k: next(replies))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = ctypes.wintypes.HANDLE
    kernel.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    # Allow readers/writers but deny deletion/replacement of the destination.
    handle = kernel.CreateFileW(str(queue_file), 0x80000000, 3, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        assert sidecar.queue_flush("http://fixture.invalid", "synthetic-key") == 1
        assert queue_file.read_bytes() == original
        assert list(tmp_path.iterdir()) == [queue_file]
    finally:
        assert kernel.CloseHandle(handle)
    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", lambda *a, **k: (True, {}, 200))
    assert sidecar.queue_flush("http://fixture.invalid", "synthetic-key") == 3
    assert not queue_file.exists()
