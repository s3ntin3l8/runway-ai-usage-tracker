"""The managed TestClient must keep one event loop for its whole life.

A bare ``TestClient`` opens and tears down a fresh event loop *per request*,
and every event loop's self-pipe is a socketpair: two sockets per request,
all parked in TIME_WAIT for 120 seconds. A full suite run created enough of
them to fill the Windows ephemeral-port range, and a second run on the same
machine then could not open a socket at all (``WinError 10055``).
"""

import socket
from collections.abc import Callable

from starlette import testclient

from tests.fixtures.test_client import TestClient


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
