"""The real transport talks only to synthetic subprocesses, never Security.framework."""

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.sidecar_pkg import macos_keychain_store as store

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Darwin native transport uses POSIX pipes"
)


@pytest.fixture
def client_factory(tmp_path):
    clients = []

    def make(mode="echo", timeout=1):
        from scripts.sidecar_pkg.keychain_client import KeychainClient

        script = tmp_path / (mode + ".py")
        script.write_text("""import os,sys,time,struct,plistlib
while True:
    header=sys.stdin.buffer.read(4)
    if not header: break
    request=plistlib.loads(sys.stdin.buffer.read(struct.unpack('>I',header)[0]))
    mode=sys.argv[1]
    if mode=='hang': time.sleep(20)
    if mode=='eof': sys.exit(0)
    if mode=='oversize': sys.stdout.buffer.write(struct.pack('>I',4194305)); sys.stdout.buffer.flush(); continue
    if mode=='malformed': data=b'private-secret'
    else: data=plistlib.dumps({'error':'denied'} if mode=='deny' else {'result':{'pid':os.getpid(),'value':request['query']}})
    frame=struct.pack('>I',len(data))+data
    for byte in frame:
        sys.stdout.buffer.write(bytes([byte])); sys.stdout.buffer.flush()
""")
        client = KeychainClient(command=[sys.executable, str(script), mode], timeout=timeout)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def test_many_operations_reuse_one_worker(client_factory):
    client = client_factory()
    results = [client.call("copy", {"index": i}, {}, group="code") for i in range(21)]
    assert len({r["pid"] for r in results}) == 1
    assert [r["value"]["index"] for r in results] == list(range(21))


def test_concurrent_frames_are_not_interleaved(client_factory):
    client = client_factory()

    def call(i):
        return client.call("copy", {"index": i}, {}, group="code")["value"]["index"]

    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(call, range(16))) == list(range(16))


@pytest.mark.parametrize(
    "mode,reason,timeout",
    [
        # The child sleeps 20 s before it answers, so the client's own deadline
        # has to expire first - and that does not depend on the child booting.
        ("hang", "timed_out", 0.15),
        # These children answer as soon as they are up. The client's budget
        # covers the spawn *and* the cold interpreter start, so it has to be
        # wide enough for that on a loaded machine; otherwise the call ends on
        # its own deadline ("timed_out") instead of reporting the child's
        # failure.
        ("eof", "unavailable", 5),
        ("oversize", "unavailable", 5),
        ("malformed", "unavailable", 5),
        ("deny", "denied", 5),
    ],
)
def test_failed_worker_is_retired_and_group_latched(client_factory, mode, reason, timeout):
    client = client_factory(mode, timeout=timeout)
    with pytest.raises(store.KeychainError, match="^" + reason + "$"):
        client.call("copy", {}, {}, group="code")
    assert client.access_blocked("code")
    with pytest.raises(store.KeychainError):
        client.call("copy", {}, {}, group="code")


def test_denial_groups_are_independent(client_factory):
    client = client_factory()
    client.block_access("code")
    with pytest.raises(store.KeychainError):
        client.call("copy", {}, {}, group="code")
    assert client.call("copy", {}, {}, group="storage")["pid"] > 0


def test_permission_rechecked_after_waiting_for_request_lock(client_factory):
    client = client_factory()
    allowed = [True]
    started = threading.Event()

    def mutate():
        started.set()
        return client.call(
            "update", {}, {"data": b"synthetic"}, group="code", allowed=lambda: allowed[0]
        )

    with client._request_lock:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(mutate)
            assert started.wait(1)
            allowed[0] = False
            client._request_lock.release()
            try:
                with pytest.raises(store.KeychainError, match="permission disabled"):
                    future.result(1)
            finally:
                client._request_lock.acquire()
    assert client._process is None


def test_shutdown_interrupts_active_io(client_factory):
    import time

    # The child's "hang" mode sleeps 20s before it answers, so a client
    # timeout below that is what makes an uninterrupted call fail with
    # "timed_out" instead of succeeding. The difference between that and
    # "cancelled" is what the assertion below turns on.
    timeout = 10
    client = client_factory("hang", timeout=timeout)
    # The wait for the worker has to outlast the client's own deadline, so a
    # call that dies on that deadline is reported as a call that ended rather
    # than as a worker that never came up.
    worker_wait = timeout + 5

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client.call, "copy", {}, {}, group="code")

        # A shutdown can only interrupt a request that is still in flight, so
        # wait for the worker to come up first - but not past the call itself:
        # if the call has already ended, there is no in-flight request left to
        # interrupt.
        deadline = time.monotonic() + worker_wait
        while client._process is None:
            if future.done():
                pytest.fail(
                    f"the call ended before the shutdown could interrupt it: {future.exception()!r}"
                )
            if time.monotonic() >= deadline:
                pytest.fail("the keychain worker never started")
            time.sleep(0.005)
        process = client._process

        client.close()

        # Every path a shutdown can take ends in "cancelled": close() sets
        # _closed and bumps _generation, so KeychainClient._cancelled() holds
        # in both the OSError handler and the KeychainError handler. Asserting
        # that reason is what tells "the shutdown interrupted the call" apart
        # from "the call timed out on its own" -- and the latter is a
        # KeychainError too, so raising any KeychainError would not.
        try:
            future.result(timeout)
        except TimeoutError:
            pytest.fail(f"the shutdown did not interrupt the call within {timeout}s")
        except store.KeychainError as exc:
            assert exc.reason == "cancelled", (
                f"the call ended with reason {exc.reason!r}; the shutdown has to "
                "interrupt the request, not time it out"
            )
        else:
            pytest.fail("the call succeeded; the shutdown did not interrupt it")
        assert process.poll() is not None


def test_waiting_timeout_does_not_kill_another_request(client_factory):
    # The client's deadline is 0.2 s, so the wait for its outcome has to
    # outlast that: on a loaded machine the thread that raises the timeout can
    # be scheduled late, and a bare TimeoutError here would hide the reason
    # the call actually ended with.
    client = client_factory("hang", timeout=0.2)
    with ThreadPoolExecutor(1) as pool:
        with client._request_lock:
            future = pool.submit(client.call, "copy", {}, {}, group="other")
            with pytest.raises(store.KeychainError, match="timed_out"):
                future.result(5)
        assert client._process is None
        assert not client.access_blocked("other")


def test_stalled_request_write_is_bounded(client_factory):
    import time

    client = client_factory(timeout=0.15)
    client._command = [sys.executable, "-c", "import time; time.sleep(20)"]
    start = time.monotonic()
    with pytest.raises(store.KeychainError, match="timed_out"):
        client.call("update", {}, {"data": b"x" * 1000000}, group="code", allowed=lambda: True)
    assert time.monotonic() - start < 2
    assert client._process is None


def test_failed_protocol_does_not_leak_payload(client_factory):
    client = client_factory("malformed")
    with pytest.raises(store.KeychainError) as caught:
        client.call("copy", {"service": "synthetic-private"}, {}, group="code")
    assert str(caught.value) == "unavailable"
    assert "private" not in repr(caught.value)


def test_worker_native_watchdog_terminates_stalled_call(tmp_path, monkeypatch):
    from scripts.sidecar_pkg.keychain_client import KeychainClient

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    marker = tmp_path / "native-call-started"
    script = tmp_path / "worker.py"
    script.write_text("""import time,sys
from pathlib import Path
from scripts.sidecar_pkg import keychain_worker as w
class API:
    def call(self,*args):
        Path(sys.argv[1]).write_text("started")
        time.sleep(20)
w._NativeAPI=API
w.TIMEOUT=.1
w.worker_main()
""")
    with_cleanup = KeychainClient(command=[sys.executable, str(script), str(marker)], timeout=2)
    try:
        with pytest.raises(store.KeychainError, match="unavailable"):
            with_cleanup.call("copy", {}, {}, group="synthetic")
    finally:
        with_cleanup.close()
    assert marker.read_text() == "started"


def test_worker_exits_when_parent_dies_during_native_call(tmp_path):
    import subprocess

    marker = tmp_path / "started"
    worker = tmp_path / "stalled_worker.py"
    worker.write_text("""import sys,time
from pathlib import Path
from scripts.sidecar_pkg import keychain_worker as w
class API:
    def call(self,*args):
        Path(sys.argv[1]).write_text('started')
        time.sleep(20)
w._NativeAPI=API
w.worker_main()
""")
    controller = tmp_path / "parent.py"
    controller.write_text("""import os,subprocess,sys,time
from pathlib import Path
from scripts.sidecar_pkg.keychain_client import encode_frame
p=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]],stdin=subprocess.PIPE)
p.stdin.write(encode_frame({'operation':'copy','query':{},'values':{}})); p.stdin.flush()
deadline=time.monotonic()+2
while not Path(sys.argv[2]).exists():
    if time.monotonic()>deadline: raise RuntimeError('synthetic worker did not start')
    time.sleep(.005)
print('started',flush=True)
os._exit(0)
""")
    result = subprocess.run(
        [sys.executable, str(controller), str(worker), str(marker)],
        capture_output=True,
        timeout=4,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    assert result.returncode == 0 and result.stdout == b"started\n" and result.stderr == b""
    assert marker.read_text() == "started"


def test_lifecycle_restart_preserves_denial_groups(client_factory):
    client = client_factory()
    first = client.call("copy", {}, {}, group="healthy")["pid"]
    client.block_access("blocked")
    client.close()
    client.reopen()
    assert client.call("copy", {}, {}, group="healthy")["pid"] != first
    assert client.access_blocked("blocked")


def test_cancelled_request_does_not_latch_permission_denial(client_factory):
    import time

    timeout = 5
    client = client_factory("hang", timeout=timeout)
    # The wait for the worker has to outlast the client's own deadline, so a
    # call that dies on that deadline is reported as a call that ended rather
    # than as a worker that never came up.
    worker_wait = timeout + 5

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client.call, "copy", {}, {}, group="code")
        deadline = time.monotonic() + worker_wait
        while client._process is None:
            if future.done():
                pytest.fail(
                    f"the call ended before the shutdown could interrupt it: {future.exception()!r}"
                )
            if time.monotonic() >= deadline:
                pytest.fail("the keychain worker never started")
            threading.Event().wait(0.005)
        client.close()
        with pytest.raises(store.KeychainError, match="cancelled"):
            future.result(worker_wait)
    assert not client.access_blocked("code")


def test_pipe_setup_failure_reaps_unpublished_worker(client_factory, monkeypatch):
    import subprocess

    from scripts.sidecar_pkg import keychain_client as module

    client = client_factory()
    spawned = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", spawn)

    def fail(*args):
        raise OSError("synthetic pipe setup failure")

    monkeypatch.setattr(module.os, "set_blocking", fail)
    try:
        with pytest.raises(store.KeychainError, match="unavailable"):
            client.call("copy", {}, {}, group="code")
        assert client._process is None
        assert spawned[0].poll() is not None
    finally:
        client.close()


def test_daemon_config_reload_reopens_transport(client_factory, monkeypatch):
    from scripts import sidecar
    from scripts.sidecar_pkg import keychain_client as module

    client = client_factory()
    monkeypatch.setattr(module, "_CLIENT", client)
    first = client.call("copy", {}, {}, group="healthy")["pid"]

    class Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def join(self, **kwargs):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(sidecar.threading, "Thread", Thread)
    runner = sidecar.DaemonRunner({"api_url": "http://localhost", "api_key": "synthetic"})
    runner.stop()
    runner.start()
    assert client.call("copy", {}, {}, group="healthy")["pid"] != first
    runner.stop()


def test_failed_update_exec_reopens_transport(client_factory, monkeypatch, tmp_path):
    from scripts.sidecar_pkg import keychain_client as module
    from scripts.sidecar_pkg import self_update

    client = client_factory()
    monkeypatch.setattr(module, "_CLIENT", client)
    first = client.call("copy", {}, {}, group="healthy")["pid"]
    monkeypatch.setattr(self_update, "_systemd_restart_exit_code", lambda: None)
    monkeypatch.setattr(self_update, "_release_lock", lambda: None)
    monkeypatch.setattr(self_update, "_run_pre_exec_hooks", lambda: None)
    monkeypatch.setattr(self_update, "_undo_pre_exec_hooks", lambda: None)
    monkeypatch.setattr(self_update, "_fresh_runtime_env", lambda **kwargs: {})

    def fail(*args):
        raise OSError("synthetic exec failure")

    monkeypatch.setattr(self_update.os, "execve", fail)
    with pytest.raises(OSError, match="synthetic exec failure"):
        self_update._relaunch_posix("cli", tmp_path / "synthetic")
    assert client.call("copy", {}, {}, group="healthy")["pid"] != first
