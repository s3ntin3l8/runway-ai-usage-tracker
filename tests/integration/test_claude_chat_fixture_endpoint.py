"""Ordinary Chat observations require explicit admin intent and private body targeting."""

from unittest.mock import AsyncMock

import pytest

from app.api.endpoints import claude_chat_fixture as endpoint
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from scripts.claude_chat_evidence import empty_report
from tests.fixtures.test_client import TestClient

PATH = "/api/v1/system/debug/claude-chat-fixture"
BODY = {
    "account_id": "holder@example.invalid",
    "source_id": "fixture",
    "organization_id": "11111111-1111-4111-8111-111111111111",
    "conversation_id": "33333333-3333-4333-8333-333333333333",
}
HEADERS = {"X-Admin-Key": "chat-fixture-test-admin"}


@pytest.fixture
def client(monkeypatch):
    limiter.reset()
    monkeypatch.setattr(settings, "ADMIN_API_KEY", HEADERS["X-Admin-Key"])
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "")
    service = AsyncMock(return_value=empty_report())
    monkeypatch.setattr(endpoint, "inspect_chat_fixture", service)
    with TestClient(app, base_url="http://testserver") as http:
        yield http, service
    limiter.reset()


def test_only_explicit_admin_post_invokes_the_observer(client):
    http, service = client
    assert http.get(PATH, headers=HEADERS).status_code in (404, 405)
    assert http.post(PATH, json=BODY).status_code == 403
    service.assert_not_awaited()
    response = http.post(PATH, json=BODY, headers=HEADERS)
    assert response.status_code == 200 and response.json() == empty_report()
    assert service.call_args.args[0].conversation_id == BODY["conversation_id"]
    for identifier in BODY.values():
        assert identifier not in response.text


@pytest.mark.parametrize(
    "change",
    [
        {"conversation_id": "../../private"},
        {"organization_id": False},
        {"account_id": False},
        {"url": "https://outside.invalid/private"},
        {"cookie_sessionKey": "private-cookie"},
        {"max_requests": 4},
    ],
)
def test_invalid_body_never_echoes_identifiers_or_secret_values(client, change):
    http, service = client
    response = http.post(PATH, json=BODY | change, headers=HEADERS)
    assert response.status_code == 422 and response.json() == {"detail": "invalid_probe_request"}
    assert "private" not in response.text
    service.assert_not_awaited()


def test_cross_origin_and_bad_host_are_inert(client):
    http, service = client
    assert (
        http.post(
            PATH, json=BODY, headers=HEADERS | {"Origin": "https://outside.invalid"}
        ).status_code
        == 403
    )
    assert http.post(
        PATH, json=BODY, headers=HEADERS | {"Host": "outside.invalid"}
    ).status_code in (400, 403)
    service.assert_not_awaited()


@pytest.mark.parametrize(
    "body,status",
    [
        (b"x" * 9000, 413),
        (b"invalid", 422),
        (b'{"source_id":"one","source_id":"private"}', 422),
    ],
)
def test_bounded_strict_request_parsing_is_inert(client, body, status):
    http, service = client
    response = http.post(PATH, content=body, headers=HEADERS | {"Content-Type": "application/json"})
    assert response.status_code == status and "private" not in response.text
    service.assert_not_awaited()


def test_unauthorized_malformed_inputs_are_uniformly_rejected(client):
    http, service = client
    for body in (b"invalid", b"x" * 9000, b'{"source_id":"one","source_id":"two"}'):
        assert http.post(PATH, content=body).status_code == 403
    service.assert_not_awaited()


def test_rate_limit_stops_third_explicit_request(client):
    http, service = client
    assert [http.post(PATH, json=BODY, headers=HEADERS).status_code for _ in range(3)] == [
        200,
        200,
        429,
    ]
    assert service.await_count == 2


@pytest.mark.asyncio
async def test_disconnected_body_never_dispatches(client):
    from starlette.requests import Request

    _, service = client

    async def disconnected():
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": PATH,
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
            "headers": [
                (b"host", b"testserver"),
                (b"x-admin-key", HEADERS["X-Admin-Key"].encode()),
            ],
            "app": app,
        },
        disconnected,
    )
    route = next(
        route for route in endpoint.router.routes if route.path == "/debug/claude-chat-fixture"
    )
    response = await route.get_route_handler()(request)
    assert response.status_code == 400
    assert response.body == b'{"detail":"probe_request_disconnected"}'
    service.assert_not_awaited()
