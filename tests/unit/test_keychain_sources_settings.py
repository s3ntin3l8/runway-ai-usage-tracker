"""Local source settings endpoints must be inert and keep grants local."""

import http.client
import json
import threading

import pytest

from sidecar_app.settings_server import _SettingsServer


@pytest.fixture
def settings():
    config = {"api_url": "https://runway.example", "api_key": "synthetic-private"}
    server = _SettingsServer(
        "127.0.0.1",
        0,
        lambda: config,
        lambda: {},
        lambda value: config.update(value),
        lambda: None,
        lambda: None,
        lambda: None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, config
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def send(server, method, path, body=None, origin=True):
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = f"http://127.0.0.1:{port}"
    connection.request(
        method, path, body=json.dumps(body) if body is not None else None, headers=headers
    )
    response = connection.getresponse()
    status, raw = response.status, response.read()
    connection.close()
    return status, raw


def test_sources_get_has_no_secrets_and_does_not_grant(settings):
    server, config = settings
    status, raw = send(server, "GET", "/sources")
    assert status == 200
    assert b"synthetic-private" not in raw
    sources = json.loads(raw)["sources"]
    assert sources and all(s["state"] == "Off" for s in sources)
    assert "keychain_sources" not in config


def test_enable_all_is_read_only_and_banner_dismissal_is_not_consent(settings):
    server, config = settings
    assert (
        send(server, "POST", "/sources", {"action": "dismiss", "source_id": "anthropic.chrome"})[0]
        == 200
    )
    assert not config.get("keychain_sources")
    assert send(server, "POST", "/sources", {"action": "enable_all"})[0] == 200
    assert all(entry["read"] is True for entry in config["keychain_sources"].values())
    assert not config.get("claude_keychain_renewal")


def test_source_post_requires_same_origin_and_rejects_unknown_or_truthy_values(settings):
    server, config = settings
    # The Origin guard rejects on the headers alone, so this request carries no
    # payload. An HTTP/1.0 close with an unread request body resets the
    # connection on Windows, and the client would lose the 403 it asserts on
    # (WinError 10053). Same convention as tests/unit/test_sidecar_pairing.py.
    assert send(server, "POST", "/sources", origin=False)[0] == 403
    for payload in [
        {"action": "enable", "source_id": "unknown"},
        {"action": "renewal", "value": "true"},
        {"action": "authorize", "source_id": "unknown"},
    ]:
        assert send(server, "POST", "/sources", payload)[0] == 400
    assert not config.get("keychain_sources")


def test_disable_marks_only_matching_source_withdrawal_pending(settings):
    server, config = settings
    send(server, "POST", "/sources", {"action": "enable_all"})
    assert (
        send(server, "POST", "/sources", {"action": "disable", "source_id": "anthropic.chrome"})[0]
        == 200
    )
    assert config["keychain_sources"]["anthropic.chrome"]["read"] is False
    assert config["keychain_sources"]["anthropic.claude-code"]["read"] is True
    assert "anthropic.chrome" in config["keychain_withdrawals"]
