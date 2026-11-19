"""The loopback OTLP receiver for Claude Code api_request events (#1365)."""

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from scripts.sidecar_pkg.otel_receiver import OtelReceiver

EMAIL = "someone@example.com"


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": value}}


def _record(request_id: str | None, query_source: str, **extra: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "event.name": "api_request",
        "model": "claude-haiku-5-5-20260101",
        "input_tokens": 12,
        "output_tokens": 34,
        "cache_read_tokens": 5,
        "cache_creation_tokens": 30,
        "cache_creation_5m_tokens": 10,
        "cache_creation_1h_tokens": 20,
        "cost_usd": 0.0123,
        "query_source": query_source,
        "speed": "normal",
        "effort": "high",
        "session.id": "sess-1",
        "event.timestamp": "2026-11-18T10:00:00.000Z",
        "user.email": EMAIL,
        "organization.id": "org-secret",
    }
    if request_id is not None:
        attrs["request_id"] = request_id
    attrs.update(extra)
    return {"attributes": [_attr(k, v) for k, v in attrs.items() if v is not None]}


def _payload(*records: dict[str, Any]) -> bytes:
    return json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]}).encode()


def _post(
    receiver: OtelReceiver,
    body: bytes,
    path: str = "/v1/logs",
    content_type: str = "application/json",
) -> tuple[int, bytes]:
    host, port = receiver.address
    request = urllib.request.Request(
        f"http://{host}:{port}{path}", data=body, headers={"Content-Type": content_type}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def receiver() -> Iterator[OtelReceiver]:
    instance = OtelReceiver(port=0)
    assert instance.start()
    yield instance
    instance.stop()


def test_only_side_calls_are_kept_and_nothing_identifying(
    receiver: OtelReceiver, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    body = _payload(
        _record("req_title", "generate_session_title"),
        _record("req_main", "repl_main_thread"),
        _record("req_agent", "agent:builtin:Explore"),
        _record(None, "generate_session_title"),
        {"attributes": [_attr("event.name", "user_prompt"), _attr("request_id", "req_x")]},
    )
    assert _post(receiver, body) == (200, b"{}")

    events = receiver.drain("acct@example.org", "local")
    assert len(events) == 1
    event = events[0]
    assert event["event_id"] == "otel|req_title"
    assert event["request_id"] == "req_title"
    assert event["query_source"] == "generate_session_title"
    assert event["provider_id"] == "anthropic"
    assert event["source_app"] == "claude_code"
    assert event["account_id"] == "acct@example.org"
    assert event["model_id"] == "haiku-5.5"
    assert event["session_id"] == "sess-1"
    assert event["ts"] == "2026-11-18T10:00:00+00:00"
    assert (event["tokens_input"], event["tokens_output"], event["tokens_cache_read"]) == (
        12,
        34,
        5,
    )
    assert event["tokens_cache_create"] == 30
    assert (event["tokens_cache_create_1h"], event["tokens_cache_create_5m"]) == (20, 10)
    assert event["speed"] == "normal" and event["effort"] == "high"
    assert event["cost_usd"] is None
    dumped = json.dumps(event) + caplog.text
    for secret in (EMAIL, "org-secret", "0.0123"):
        assert secret not in dumped
    assert receiver.filtered == 2 and receiver.dropped_incomplete == 1


def test_unknown_query_source_is_captured_and_bad_timestamp_dropped(
    receiver: OtelReceiver,
) -> None:
    body = _payload(
        _record("req_new", "some_future_source"),
        _record("req_bad_ts", "compact", **{"event.timestamp": "yesterday"}),
    )
    assert _post(receiver, body)[0] == 200
    events = receiver.drain("default")
    assert [e["event_id"] for e in events] == ["otel|req_new"]
    assert receiver.dropped_incomplete == 1


def test_wrong_path_content_type_and_bad_body(receiver: OtelReceiver) -> None:
    assert _post(receiver, b"{}", path="/v1/traces")[0] == 404
    assert _post(receiver, b"{}", content_type="application/x-protobuf")[0] == 415
    assert _post(receiver, b"not json")[0] == 400
    assert _post(receiver, b"[1, 2]")[0] == 200  # valid JSON, nothing to keep
    assert receiver.pending() == 0


def test_get_is_not_found(receiver: OtelReceiver) -> None:
    host, port = receiver.address
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(f"http://{host}:{port}/v1/logs", timeout=5)
    assert caught.value.code == 404


def test_binds_loopback_only(receiver: OtelReceiver) -> None:
    host, port = receiver.address
    assert host == "127.0.0.1" and port > 0


def test_queue_cap_drops_oldest_and_counts() -> None:
    receiver = OtelReceiver(port=0, queue_cap=2)
    from scripts.sidecar_pkg.otel_receiver import parse_logs

    for index in range(3):
        receiver.accept(parse_logs(json.loads(_payload(_record(f"req_{index}", "compact")))))
    assert receiver.dropped_overflow == 1
    assert [e["request_id"] for e in receiver.drain("default")] == ["req_1", "req_2"]


def test_requeue_restores_events_and_ignores_others() -> None:
    receiver = OtelReceiver(port=0, queue_cap=10)
    from scripts.sidecar_pkg.otel_receiver import parse_logs

    receiver.accept(parse_logs(json.loads(_payload(_record("req_a", "compact")))))
    drained = receiver.drain("acct")
    receiver.requeue([*drained, {"event_id": "abc", "provider_id": "anthropic"}])
    assert [e["event_id"] for e in receiver.drain("acct")] == ["otel|req_a"]


def test_port_in_use_warns_and_does_not_raise(
    receiver: OtelReceiver, caplog: pytest.LogCaptureFixture
) -> None:
    _, port = receiver.address
    second = OtelReceiver(port=port)
    with caplog.at_level(logging.WARNING):
        assert second.start() is False
    assert second.address is None
    assert [r.levelno for r in caplog.records].count(logging.WARNING) == 1
    second.stop()  # harmless when never started
