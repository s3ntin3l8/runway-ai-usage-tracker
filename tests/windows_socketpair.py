"""A socketpair that cannot hang, for the Windows test session.

CPython's Windows fallback (``socket._fallback_socketpair``) connects with a
non-blocking socket, swallows the ``BlockingIOError`` when the connect is still
pending, and then blocks in ``accept()`` forever if the handshake never lands:

```python
    csock.setblocking(False)
    try:
        csock.connect((addr, port))
    except (BlockingIOError, InterruptedError):
        pass          # <-- the connection may not have gone out yet
    csock.setblocking(True)
    ssock, _ = lsock.accept()   # <-- blocking, with no timeout
```

Both asyncio event loops build their self-pipe with ``socket.socketpair()`` --
``selector_events._make_self_pipe`` and ``proactor_events._make_self_pipe`` --
so a worker that stalls there never reports, xdist prints ``node down: Not
properly terminated``, and the master's loadfile scheduler dies with a
``KeyError`` on the collection the lost worker never registered (#830).

This implementation connects in blocking mode, so the loopback handshake
completes before ``accept()`` is called and there is no pending connect to
wait on. The listening socket carries a timeout, so a handshake that somehow
never lands becomes a loud ``socket.timeout`` instead of an endless wait.

A crowded machine can refuse a source port outright: Windows raises
``WSAEADDRINUSE`` (WinError 10048) from ``connect()`` when it cannot hand the
client socket a port that is free, and ``WSAENOBUFS`` (WinError 10055) when
there is no buffer space for one. An IP Helper ``portproxy`` rule that
forwards a port to itself can eat the machine's whole dynamic range that way
(see ``CONTRIBUTING.md``). The pair is rebuilt for a fresh source port,
because a fresh socket gets a fresh port. Every other failure — a timeout,
a refused peer, a bad argument — still surfaces at once.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from typing import Any

#: How long the listening socket may wait for the handshake to land.
ACCEPT_TIMEOUT_SECONDS = 10.0

#: Windows socket errors a crowded machine raises transiently, and that a
#: fresh socket clears: the source port is taken, or there is no buffer space
#: for one. Anything else is a real failure and must surface at once.
_RETRYABLE_WINERRORS = frozenset({10048, 10055})  # WSAEADDRINUSE, WSAENOBUFS

#: How many pairs to attempt before the last error is raised.
_PAIR_ATTEMPTS = 5

#: The wait between attempts, doubling each time.
_PAIR_RETRY_BACKOFF_SECONDS = 0.05

#: The loopback addresses CPython's own fallback uses.
_LOCALHOST = "127.0.0.1"
_LOCALHOST_V6 = "::1"

#: Set to "1" to leave CPython's socketpair in place (for A/B debugging).
DISABLE_ENV_VAR = "RUNWAY_DISABLE_WINDOWS_SOCKETPAIR"


def windows_socketpair(
    family: int = socket.AF_INET,
    type: int = socket.SOCK_STREAM,  # noqa: A002 — the stdlib's own parameter name
    proto: int = 0,
) -> tuple[socket.socket, socket.socket]:
    """A connected socket pair, made without CPython's hang window.

    The pair is a loopback TCP connection, exactly what CPython's fallback
    returns on Windows, and the peer is authenticated the same way: a
    connection from anything else that reached the same port is refused.

    A transient port error from a crowded machine is retried on a fresh
    socket; everything else surfaces at once.
    """
    if family == socket.AF_INET:
        host = _LOCALHOST
    elif family == socket.AF_INET6:
        host = _LOCALHOST_V6
    else:
        raise ValueError("Only AF_INET and AF_INET6 socket address families are supported")
    if type != socket.SOCK_STREAM:
        raise ValueError("Only SOCK_STREAM socket type is supported")
    if proto != 0:
        raise ValueError("Only protocol zero is supported")

    # The machine can hand the client socket a source port that is already
    # bound, and the connect then fails with WSAEADDRINUSE. That is the
    # machine's condition, not the suite's, so the pair is rebuilt for a
    # fresh source port instead of failing the run over it.
    last: OSError | None = None
    for attempt in range(_PAIR_ATTEMPTS):
        try:
            return _make_pair(family, type, proto, host)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in _RETRYABLE_WINERRORS:
                raise
            last = exc
            if attempt < _PAIR_ATTEMPTS - 1:
                time.sleep(_PAIR_RETRY_BACKOFF_SECONDS * (2**attempt))

    # Every attempt failed with a retryable error: the last one is the
    # truth, and a machine that cannot spare a port must say so loudly.
    if last is not None:
        raise last
    raise RuntimeError("the socketpair was never attempted")  # pragma: no cover


def _make_pair(
    family: int,
    type: int,  # noqa: A002 — the stdlib's own parameter name
    proto: int,
    host: str,
) -> tuple[socket.socket, socket.socket]:
    """One attempt at a connected pair. The caller decides whether to try again."""
    lsock = socket.socket(family, type, proto)
    try:
        lsock.bind((host, 0))
        lsock.listen()
        # On IPv6, ignore flow_info and scope_id
        addr, port = lsock.getsockname()[:2]
        # The timeout turns a handshake that never lands into a loud failure
        # instead of the endless wait CPython's fallback can fall into.
        lsock.settimeout(ACCEPT_TIMEOUT_SECONDS)
        csock = socket.socket(family, type, proto)
        try:
            # The connect blocks: the loopback handshake completes here, so
            # the accept below never has to wait for a connect still in flight.
            csock.connect((addr, port))
            ssock, _ = lsock.accept()
        except Exception:
            # The accept is inside this try on purpose: a handshake that
            # times out has to close the client socket too, or it leaks a
            # descriptor for every pair that fails.
            csock.close()
            raise
    finally:
        lsock.close()

    # The sockets are handed back in blocking mode with no timeout, the way
    # CPython's fallback hands them back.
    ssock.settimeout(None)
    csock.settimeout(None)

    # Authenticating avoids using a connection from something else
    # able to connect to {host}:{port} instead of us.
    # We expect only AF_INET and AF_INET6 families.
    try:
        if ssock.getsockname() != csock.getpeername() or csock.getsockname() != ssock.getpeername():
            raise ConnectionError("Unexpected peer connection")
    except Exception:
        # getsockname() and getpeername() can fail
        # if either socket isn't connected.
        ssock.close()
        csock.close()
        raise

    return (ssock, csock)


def has_native_socketpair() -> bool:
    """Whether this CPython ships a native socketpair."""
    native: Any = getattr(socket, "_socket", None)
    return native is not None and hasattr(native, "socketpair")


def install_windows_socketpair() -> bool:
    """Replace ``socket.socketpair`` with one that cannot hang.

    Only on Windows, and only for a CPython that has no native socketpair
    there -- a build that ships one is left alone. Returns whether the
    replacement was installed.
    """
    if os.environ.get(DISABLE_ENV_VAR) == "1":
        return False
    if sys.platform != "win32":
        return False
    if has_native_socketpair():
        # This CPython ships a native socketpair: nothing to replace.
        return False
    socket.socketpair = windows_socketpair
    return True
