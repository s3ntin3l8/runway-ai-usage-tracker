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
"""

from __future__ import annotations

import os
import socket
import sys
from typing import Any

#: How long the listening socket may wait for the handshake to land.
ACCEPT_TIMEOUT_SECONDS = 10.0

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
        except Exception:
            csock.close()
            raise
        ssock, _ = lsock.accept()
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
    native: Any = getattr(socket, "_socket", None)
    if native is not None and hasattr(native, "socketpair"):
        # This CPython ships a native socketpair: nothing to replace.
        return False
    socket.socketpair = windows_socketpair
    return True
