"""A TestClient that keeps one event loop for its whole life.

A bare ``TestClient`` opens and tears down a fresh event loop *per request* —
its transport calls ``_portal_factory()`` around every call, and a client
that was never entered has no portal of its own — and every event loop's
self-pipe is a socketpair: two sockets per request, all parked in
TIME_WAIT for 120 seconds. A full unit run created ~16k of them, which is
the entire Windows ephemeral-port range, so a second run on the same
machine could not open a socket at all (``WinError 10055``).

Entering the client once keeps a single portal — and a single event loop —
for every request it makes, which is what ``with TestClient(app) as c:``
does. Doing it here means every call site gets it without a context
manager, and the app's lifespan runs once per client rather than once per
request.

**One loop per worker, not one per client.** A loop is a socketpair, and a
socketpair is two sockets in TIME_WAIT for 120 seconds, so a loop per client
is a few thousand socketpairs per suite run — and they accumulate across
runs, which is how the range filled in the first place. The worker's
clients share one loop instead: one socketpair per worker, for the whole
run.

Nothing is left behind on that loop. Measured over the unit suite, the
loop runs a constant two tasks — the loop's own and the call's — so a test
does not leak work into the next one, and closing the loop per client was
cancelling tasks that were not there.
"""

from __future__ import annotations

from typing import Any

import anyio
from fastapi import testclient as _fastapi_testclient

#: The worker's event loop, and the portal that owns it. Created on the
#: first client and closed when the worker finishes.
_PORTAL_STACK: Any | None = None
_PORTAL: Any | None = None

#: Every client a test created, so the conftest fixture can close them.
_OPEN_CLIENTS: list[TestClient] = []


def _shared_portal(backend: Any) -> Any:
    """The worker's one event loop, created on first use.

    The backend is the first client's: the loop is the worker's, so the
    first one to ask for it decides which backend it runs on.
    """
    global _PORTAL_STACK, _PORTAL
    if _PORTAL is None:
        _PORTAL_STACK = anyio.from_thread.start_blocking_portal(**backend)
        _PORTAL = _PORTAL_STACK.__enter__()
    return _PORTAL


class TestClient(_fastapi_testclient.TestClient):
    """A TestClient that holds one event loop for its whole life."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The worker's portal, shared by every client. The transport's
        # `_portal_factory` reuses `self.portal` when it is set, so every
        # request runs on this one event loop instead of starting a new one.
        #
        # The client's lifespan is deliberately *not* started: a bare
        # TestClient never runs it either, and starting it would run the
        # app's startup handlers (and their side effects) where the tests
        # expect them not to run.
        self.portal = _shared_portal(self.async_backend)
        _OPEN_CLIENTS.append(self)

    def shutdown(self) -> None:
        """Detach the client from the shared event loop.

        The loop itself belongs to the worker, not to the client: closing
        it per client is what filled the ephemeral-port range in the first
        place. It is closed once, when the worker finishes.
        """
        self.portal = None


def close_open_clients() -> None:
    """Release every client a test created.

    The clients are detached from the shared loop. The loop is not closed
    here — it belongs to the worker — and nothing needs cancelling: the
    shared loop runs a constant two tasks, the loop's own and the call's,
    so a test leaves nothing behind for the next one.
    """
    while _OPEN_CLIENTS:
        client = _OPEN_CLIENTS.pop()
        try:
            client.shutdown()
        except Exception:  # pragma: no cover - teardown must not fail a test
            pass


def close_shared_portal() -> None:
    """Close the worker's event loop. Called once, when the worker ends."""
    global _PORTAL_STACK, _PORTAL
    stack, _PORTAL_STACK, _PORTAL = _PORTAL_STACK, None, None
    if stack is not None:
        try:
            stack.__exit__(None, None, None)
        except Exception:  # pragma: no cover - teardown must not fail a test
            pass
