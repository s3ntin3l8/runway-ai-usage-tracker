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
"""

from __future__ import annotations

from typing import Any

import anyio
from fastapi import testclient as _fastapi_testclient

#: Every client a test created, so the conftest fixture can close them.
_OPEN_CLIENTS: list[TestClient] = []


class TestClient(_fastapi_testclient.TestClient):
    """A TestClient that holds one event loop for its whole life."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Start one portal and hand it to the transport. The transport's
        # `_portal_factory` reuses `self.portal` when it is set, so every
        # request runs on this one event loop instead of starting a new one.
        #
        # The client's lifespan is deliberately *not* started: a bare
        # TestClient never runs it either, and starting it would run the
        # app's startup handlers (and their side effects) where the tests
        # expect them not to run.
        self._portal_stack = anyio.from_thread.start_blocking_portal(**self.async_backend)
        self.portal = self._portal_stack.__enter__()
        _OPEN_CLIENTS.append(self)

    def __enter__(self) -> TestClient:
        # Already started in __init__ — entering twice would start a second
        # portal and leave the first one running.
        return self

    def shutdown(self) -> None:
        """Close the client and its event loop."""
        portal_stack = getattr(self, "_portal_stack", None)
        if portal_stack is not None:
            self._portal_stack = None
            self.portal = None
            portal_stack.__exit__(None, None, None)


def close_open_clients() -> None:
    """Close every client a test created."""
    while _OPEN_CLIENTS:
        client = _OPEN_CLIENTS.pop()
        try:
            client.shutdown()
        except Exception:  # pragma: no cover - teardown must not fail a test
            pass
