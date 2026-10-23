"""The managed TestClient must keep one event loop for its whole life.

A bare ``TestClient`` opens and tears down a fresh event loop *per request*,
and every event loop's self-pipe is a socketpair: two sockets per request,
all parked in TIME_WAIT for 120 seconds. A full suite run created enough of
them to fill the Windows ephemeral-port range, and a second run on the same
machine then could not open a socket at all (``WinError 10055``).
"""

import asyncio
import socket
import time
from collections.abc import Callable

import anyio
from starlette import testclient

from tests.fixtures.test_client import TestClient, close_portal


def _count_socketpairs(make_requests: Callable[[], None]) -> int:
    """How many event loops (and so socket pairs) the requests needed."""
    created = 0
    original = socket.socketpair

    def counting(*args, **kwargs):
        nonlocal created
        created += 1
        return original(*args, **kwargs)

    socket.socketpair = counting
    try:
        make_requests()
    finally:
        socket.socketpair = original
    return created


def _ping_app():
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_many_requests_share_one_event_loop():
    app = _ping_app()
    client = TestClient(app)
    try:

        def make_requests() -> None:
            for _ in range(20):
                assert client.get("/ping").status_code == 200

        # One loop for the client and all twenty of its requests.
        assert _count_socketpairs(make_requests) == 0
    finally:
        client.shutdown()


def test_a_bare_client_would_use_one_event_loop_per_request():
    # The counter-mandated baseline: without the managed client this would be
    # 20 — which is what filled the ephemeral-port range.
    app = _ping_app()
    client = testclient.TestClient(app)
    try:

        def make_requests() -> None:
            for _ in range(20):
                assert client.get("/ping").status_code == 200

        assert _count_socketpairs(make_requests) == 20
    finally:
        client.close()


def test_many_clients_share_one_event_loop():
    """One event loop for every client in the worker.

    An event loop is a socket pair, and the Windows ephemeral-port range is
    about 16k sockets. A loop per client filled it -- a few thousand per
    suite run, and they accumulate across runs -- and the run after that
    could not open a socket at all. One loop for the whole worker is one
    socket pair for the whole run.
    """
    app = _ping_app()
    clients = [TestClient(app) for _ in range(20)]
    try:
        assert len({id(client.portal) for client in clients}) == 1
    finally:
        for client in clients:
            client.shutdown()


def test_many_clients_need_at_most_one_socket_pair():
    """The same property, measured in socket pairs.

    One socket pair for twenty clients -- or none, if the worker's loop was
    already up, which is the normal case by the time this test runs.
    """
    app = _ping_app()

    def make_clients() -> None:
        for _ in range(20):
            client = TestClient(app)
            try:
                assert client.get("/ping").status_code == 200
            finally:
                client.shutdown()

    assert _count_socketpairs(make_clients) <= 1


def test_the_with_form_does_not_start_the_app_lifespan():
    """``with TestClient(app) as c:`` must not run the app's startup.

    Starlette's own ``__enter__`` starts a portal *and* runs the lifespan.
    The managed client already has its portal, so entering it is a no-op --
    otherwise the documented ``with`` form would run the app's startup
    handlers, which is the one thing this fixture must not do.
    """
    from fastapi import FastAPI

    started: list[str] = []
    app = FastAPI()

    @app.on_event("startup")
    async def _startup() -> None:
        started.append("startup")

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
    assert started == []


def test_closing_a_portal_with_a_leaked_task_does_not_hang():
    """A task that outlives its test must not wedge the worker at teardown.

    Closing a loop with live tasks waits for them, and a task that never
    finishes would wait forever -- the same failure this change exists to
    remove. The remaining tasks are cancelled first, so the close is quick.
    """
    stack = anyio.from_thread.start_blocking_portal()
    portal = stack.__enter__()

    async def leak() -> None:
        await asyncio.sleep(3600)

    portal.start_task_soon(leak)
    assert close_portal(stack, portal, timeout=10) is True


def test_closing_a_portal_whose_task_blocks_the_loop_is_bounded():
    """A task the loop cannot interrupt must not wedge the teardown either.

    The cancel is best effort -- a blocking call in the loop thread cannot
    be interrupted -- so the close has a bound of its own: it reports that
    it ran out of time rather than waiting forever.
    """
    stack = anyio.from_thread.start_blocking_portal()
    portal = stack.__enter__()

    async def blocked() -> None:
        # A blocking call: the cancellation cannot be delivered until it
        # returns, so the loop thread stays stuck.
        time.sleep(10)

    portal.start_task_soon(blocked)
    assert close_portal(stack, portal, timeout=2) is False


def test_the_managed_client_does_not_start_the_app_lifespan():
    # A bare TestClient never ran the app's startup handlers, and tests rely
    # on that: running them would bootstrap state the tests set up themselves.
    from fastapi import FastAPI

    started: list[str] = []
    app = FastAPI()

    @app.on_event("startup")
    async def _startup() -> None:
        started.append("startup")

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    try:
        assert client.get("/ping").status_code == 200
        assert started == []
    finally:
        client.shutdown()
