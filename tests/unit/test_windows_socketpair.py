"""The socketpair the Windows test session runs on cannot hang.

CPython's Windows fallback connects without blocking and then blocks in
``accept()``, so a worker stalled there never reports and xdist's master dies
with a ``KeyError`` on the collection the lost worker never registered (#830).
The replacement ``tests/conftest.py`` installs connects in blocking mode, so
the handshake completes before the accept.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.windows_socketpair import (
    DISABLE_ENV_VAR,
    has_native_socketpair,
    install_windows_socketpair,
    windows_socketpair,
)

# These are the tests whose subject is the Windows platform, so the native
# Windows gate runs only these (see `scripts/dev_checks.py test-windows`).
pytestmark = pytest.mark.windows


def test_the_session_socketpair_returns_two_connected_sockets():
    """Whatever is installed has to be a pair that carries data."""
    left, right = socket.socketpair()
    try:
        left.sendall(b"ping")
        assert right.recv(4) == b"ping"
        right.sendall(b"pong")
        assert left.recv(4) == b"pong"
    finally:
        left.close()
        right.close()


def test_a_burst_of_socketpairs_does_not_stall():
    """The stall was a window in the connect/accept sequence, not a cost.

    The count is modest on purpose: every pair leaves two sockets in
    TIME_WAIT, and the Windows ephemeral-port range is the suite's tightest
    resource (see ``tests/fixtures/test_client.py``). The budget is per pair,
    so a stall names the pair that stalled instead of failing a total the
    loaded runner might miss.
    """
    for index in range(100):
        started = time.monotonic()
        left, right = socket.socketpair()
        left.close()
        right.close()
        assert time.monotonic() - started < 5, f"socketpair {index} stalled"


def test_the_replacement_is_installed_only_where_it_is_needed():
    """Windows without a native socketpair gets the replacement."""
    if sys.platform == "win32" and not has_native_socketpair():
        assert socket.socketpair is windows_socketpair
    else:
        assert socket.socketpair is not windows_socketpair


def test_installing_is_a_no_op_off_windows(monkeypatch):
    """The install is Windows-only."""
    monkeypatch.delenv(DISABLE_ENV_VAR, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    before = socket.socketpair
    assert install_windows_socketpair() is False
    assert socket.socketpair is before


def test_the_escape_hatch_leaves_cpython_in_place():
    """``RUNWAY_DISABLE_WINDOWS_SOCKETPAIR=1`` undoes the install.

    Proved in a fresh interpreter: in this one the replacement is already
    installed, so asserting here would pass whichever way it went.
    """
    repo_root = Path(__file__).resolve().parents[2]
    script = (
        "import socket\n"
        "from tests.windows_socketpair import (\n"
        "    install_windows_socketpair,\n"
        "    windows_socketpair,\n"
        ")\n"
        "assert install_windows_socketpair() is False\n"
        "assert socket.socketpair is not windows_socketpair\n"
    )
    env = {**os.environ, DISABLE_ENV_VAR: "1", "PYTHONPATH": str(repo_root)}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_the_replacement_refuses_what_the_fallback_refused():
    """Only the loopback families, only a stream, only protocol zero."""
    with pytest.raises(ValueError):
        windows_socketpair(99)
    with pytest.raises(ValueError):
        windows_socketpair(socket.AF_INET, socket.SOCK_DGRAM)
    with pytest.raises(ValueError):
        windows_socketpair(socket.AF_INET, socket.SOCK_STREAM, 1)


def test_a_handshake_that_never_lands_fails_loudly_and_leaves_no_socket(monkeypatch):
    """The timeout is the safety net, and it must not leak a socket.

    A pair that cannot be made has to end the pair's sockets, not just the
    listening one: a leak here is a descriptor lost per failed pair.
    """
    created: list[Any] = []
    real_socket = socket.socket
    real_init: Any = real_socket.__init__

    def recording_init(self: socket.socket, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        created.append(self)

    def never_accepts(self: socket.socket, *args: Any, **kwargs: Any) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(real_socket, "__init__", recording_init)
    monkeypatch.setattr(real_socket, "accept", never_accepts)

    with pytest.raises(socket.timeout):
        windows_socketpair()

    assert created, "the pair should have been attempted"
    assert [sock.fileno() for sock in created] == [-1, -1]
