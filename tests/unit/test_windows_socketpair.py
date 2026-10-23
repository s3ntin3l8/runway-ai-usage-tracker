"""The socketpair the Windows test session runs on cannot hang.

CPython's Windows fallback connects without blocking and then blocks in
``accept()``, so a worker stalled there never reports and xdist's master dies
with a ``KeyError`` on the collection the lost worker never registered (#830).
The replacement ``tests/conftest.py`` installs connects in blocking mode, so
the handshake completes before the accept.
"""

from __future__ import annotations

import socket
import sys
import time

import pytest

from tests.windows_socketpair import (
    DISABLE_ENV_VAR,
    install_windows_socketpair,
    windows_socketpair,
)


def _has_native_socketpair() -> bool:
    """Whether this CPython ships a native socketpair."""
    native = getattr(socket, "_socket", None)
    return native is not None and hasattr(native, "socketpair")


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
    resource (see ``tests/fixtures/test_client.py``).
    """
    started = time.monotonic()
    for _ in range(100):
        left, right = socket.socketpair()
        left.close()
        right.close()
    assert time.monotonic() - started < 30


def test_the_replacement_is_installed_only_where_it_is_needed():
    """Windows without a native socketpair gets the replacement."""
    if sys.platform == "win32" and not _has_native_socketpair():
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


def test_the_escape_hatch_leaves_cpython_in_place(monkeypatch):
    """``RUNWAY_DISABLE_WINDOWS_SOCKETPAIR=1`` undoes the install."""
    monkeypatch.setenv(DISABLE_ENV_VAR, "1")
    before = socket.socketpair
    assert install_windows_socketpair() is False
    assert socket.socketpair is before


def test_the_replacement_refuses_what_the_fallback_refused():
    """Only the loopback families, only a stream, only protocol zero."""
    with pytest.raises(ValueError):
        windows_socketpair(99)
    with pytest.raises(ValueError):
        windows_socketpair(socket.AF_INET, socket.SOCK_DGRAM)
    with pytest.raises(ValueError):
        windows_socketpair(socket.AF_INET, socket.SOCK_STREAM, 1)
