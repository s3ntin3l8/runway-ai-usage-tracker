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
from scripts.sidecar_pkg import runtime_cleanup, self_update
from sidecar_app.settings_server import SettingsServer

# The native Windows gate runs the modules marked ``windows``; see
# `scripts/dev_checks.py test-windows`. These are the tests that exercise
# Windows APIs, so they are the ones a Windows runner can actually run.
pytestmark = pytest.mark.windows


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
    installed.with_name(installed.name + ".previous").write_bytes(b"older backup")
    installed.with_name(installed.name + ".previous.version").write_text("1.0.0\n")
    command = (
        "import pathlib,sys; from scripts.sidecar_pkg.self_update import _apply_windows; "
        "sys.exit(0 if _apply_windows(pathlib.Path(sys.argv[1]), "
        "pathlib.Path(sys.argv[2]), restart=False, backup_version='2.0.0') else 1)"
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
    log = install_dir / "runway-self-update.log"
    while time.monotonic() < deadline:
        try:
            if (
                installed.read_bytes() == b"replacement"
                and log.exists()
                and "Installed updated sidecar." in log.read_text()
            ):
                break
        except (FileNotFoundError, PermissionError):
            # During the bounded wait the native helper can briefly move or
            # exclusively hold the image; final assertions still require success.
            pass
        time.sleep(0.1)
    assert installed.read_bytes() == b"replacement"
    assert installed.with_name(installed.name + ".previous").read_bytes() == b"original"
    assert installed.with_name(installed.name + ".previous.version").read_text().strip() == "2.0.0"
    assert not installed.with_name(installed.name + ".previous_old").exists()
    assert not installed.with_name(installed.name + ".previous_old.version").exists()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows non-delete-sharing handle")
@pytest.mark.parametrize("operation", ["swap", "rollback"])
def test_failed_native_swap_preserves_rollback_bytes_and_version(tmp_path, monkeypatch, operation):
    installed = tmp_path / "RunwaySidecar.exe"
    previous = installed.with_name(installed.name + ".previous")
    version = installed.with_name(installed.name + ".previous.version")
    installed.write_bytes(b"current")
    previous.write_bytes(b"backup")
    version.write_text("1.0.0\n")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / installed.name).write_bytes(b"replacement")
    # Pause only initiation: all staging, the detached batch and recovery use
    # production code. The parent obstructs exactly the staged incoming file.
    command = """
import pathlib, sys
from scripts.sidecar_pkg import self_update as u
install = pathlib.Path(sys.argv[1])
u._install_path = lambda: install
u._sidecar_dir = lambda: install.parent / 'config'
u._is_frozen = lambda: True
u._is_docker = lambda: False
u.running_from_disk_image = lambda: False
original = u._apply_windows
def paused(*args, **kwargs):
    print(args[1], flush=True)
    sys.stdin.readline()
    return original(*args, **kwargs)
u._apply_windows = paused
if sys.argv[2] == 'swap':
    with u._single_flight() as owned:
        assert owned
        result = u.apply_update('tray', install.parent / 'staged', restart=False, current_version='2.0.0')
else:
    result = u.rollback('2.0.0', restart=False)
assert result  # helper staged; this is not a claim that its swap succeeded
"""
    child = subprocess.Popen(
        [sys.executable, "-c", command, str(installed), operation],
        cwd=pathlib.Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    incoming = pathlib.Path(child.stdout.readline().strip())
    assert incoming.parent == tmp_path
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
    handle = kernel.CreateFileW(str(incoming), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        _, stderr = child.communicate("\n", timeout=15)
        assert child.returncode == 0, stderr
        log = tmp_path / "runway-self-update.log"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if log.exists() and "Sidecar swap failed;" in log.read_text():
                break
            time.sleep(0.1)
        assert "New executable move failed; restoring previous sidecar." in log.read_text()
        assert "Sidecar swap failed;" in log.read_text()
        assert installed.read_bytes() == b"current"
        assert version.read_text().strip() == "1.0.0"
        assert not (tmp_path / "config" / self_update._LOCK_NAME).exists()
        if operation == "swap":
            assert previous.read_bytes() == b"backup"
        else:
            assert incoming.read_bytes() == b"backup"
    finally:
        assert kernel.CloseHandle(handle)
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    if operation == "rollback":
        monkeypatch.setattr(self_update, "_is_frozen", lambda: True)
        monkeypatch.setattr(self_update, "_install_path", lambda: installed)
        monkeypatch.setattr(self_update, "_is_docker", lambda: False)
        self_update.recover_stranded_rollback(
            now=incoming.stat().st_ctime + self_update._ROLLBACK_MAX_AGE_S + 1
        )
        assert previous.read_bytes() == b"backup"
        assert self_update.rollback_available() == "1.0.0"
    assert not installed.with_name(installed.name + ".previous_old").exists()
    assert not installed.with_name(installed.name + ".previous_old.version").exists()


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


@pytest.mark.skipif(os.name != "nt", reason="Real Windows helper process liveness")
def test_native_helper_liveness_requires_observed_process_exit():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert self_update._windows_helper_is_alive(child.pid)
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert not self_update._windows_helper_is_alive(child.pid)


@pytest.mark.skipif(os.name != "nt", reason="Real Windows process liveness")
def test_a_terminated_process_with_an_open_handle_is_not_alive():
    """A process that exited while a handle stayed open keeps its PID openable.

    ``OpenProcess`` alone therefore reports such a process as running, which is
    what left the sidecar's single-instance lock holding a dead PID after a
    self-update: every later launch read the lock, believed another sidecar was
    live and exited. The probe has to ask the process object instead.
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    kernel.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, child.pid)  # SYNCHRONIZE
    assert handle, "could not open the child process"
    try:
        child.wait(timeout=30)
        time.sleep(0.2)
        # The premise: the dead child's PID is still openable, so OpenProcess
        # alone cannot tell it from a running process.
        probe = kernel.OpenProcess(0x00100000, False, child.pid)
        assert probe, "the dead PID is expected to stay openable"
        kernel.CloseHandle(probe)
        # The probe must still report it as dead.
        assert runtime_cleanup.windows_pid_is_alive(child.pid) is False
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows PID range")
def test_a_malformed_pid_is_not_alive():
    """A corrupt lock file must be reclaimed, never crash the startup."""
    for bad in (-5, 0, 2**32, 10**20):
        assert runtime_cleanup.windows_pid_is_alive(bad) is False
