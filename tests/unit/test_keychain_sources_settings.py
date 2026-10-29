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


# --- the compact page: grouping fields, bulk selection, browsers, authorize all ---------------


def _report(server):
    return json.loads(send(server, "GET", "/sources")[1])


def test_report_describes_how_the_page_groups_each_source(settings):
    server, _config = settings
    report = _report(server)
    by_id = {s["id"]: s for s in report["sources"]}
    assert by_id["anthropic.claude-code"]["group"] == "app"
    assert by_id["anthropic.claude-code"]["service"] == "Claude Code"
    assert by_id["anthropic.chrome"]["group"] == "chrome"
    assert by_id["anthropic.chrome"]["service"] == "Claude Pro"
    assert by_id["anthropic.edge"]["group"] == "edge"
    assert by_id["anthropic.chromium"]["group"] == "chromium"
    # One Keychain item is read by every service in the same browser.
    assert by_id["anthropic.chrome"]["item"] == by_id["chatgpt.chrome"]["item"]
    assert by_id["anthropic.chrome"]["item"] != by_id["anthropic.edge"]["item"]
    assert report["browsers"] == {"chrome": True, "edge": True, "chromium": True}
    assert report["run"] == {
        "active": False,
        "total": 0,
        "done": 0,
        "current": [],
        "queued": [],
        "cancelled": False,
    }


def test_set_sources_turns_several_sources_on_or_off_in_one_request(settings):
    server, config = settings
    ids = ["anthropic.chrome", "chatgpt.chrome"]
    assert (
        send(
            server, "POST", "/sources", {"action": "set_sources", "source_ids": ids, "value": True}
        )[0]
        == 200
    )
    assert {i for i, e in config["keychain_sources"].items() if e["read"]} == set(ids)
    assert not config.get("keychain_withdrawals")
    assert (
        send(
            server, "POST", "/sources", {"action": "set_sources", "source_ids": ids, "value": False}
        )[0]
        == 200
    )
    assert not any(e["read"] for e in config["keychain_sources"].values())
    assert set(config["keychain_withdrawals"]) == set(ids)


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "set_sources", "source_ids": [], "value": True},
        {"action": "set_sources", "source_ids": "anthropic.chrome", "value": True},
        {"action": "set_sources", "source_ids": ["nope.nope"], "value": True},
        {"action": "set_sources", "source_ids": [1], "value": True},
        {"action": "set_sources", "source_ids": ["anthropic.chrome"], "value": "true"},
        {"action": "set_browsers", "value": {}},
        {"action": "set_browsers", "value": {"firefox": True}},
        {"action": "set_browsers", "value": {"chrome": "yes"}},
        {"action": "set_browsers", "value": "chrome"},
    ],
)
def test_bulk_actions_reject_malformed_requests_without_changing_anything(settings, payload):
    server, config = settings
    assert send(server, "POST", "/sources", payload)[0] == 400
    assert not config.get("keychain_sources") and "keychain_browsers" not in config


def test_unticking_a_browser_turns_off_its_sources_and_keeps_the_choice(settings):
    server, config = settings
    send(server, "POST", "/sources", {"action": "enable_all"})
    assert (
        send(server, "POST", "/sources", {"action": "set_browsers", "value": {"edge": False}})[0]
        == 200
    )
    assert config["keychain_browsers"] == {"chrome": True, "edge": False, "chromium": True}
    edge = [i for i in config["keychain_sources"] if i.endswith(".edge")]
    assert edge and all(config["keychain_sources"][i]["read"] is False for i in edge)
    assert set(edge) <= set(config["keychain_withdrawals"])
    chrome = [i for i in config["keychain_sources"] if i.endswith(".chrome")]
    assert all(config["keychain_sources"][i]["read"] is True for i in chrome)
    assert config["keychain_sources"]["anthropic.claude-code"]["read"] is True
    assert _report(server)["browsers"]["edge"] is False


def test_enable_all_leaves_unticked_browsers_off(settings):
    server, config = settings
    send(server, "POST", "/sources", {"action": "set_browsers", "value": {"chromium": False}})
    send(server, "POST", "/sources", {"action": "enable_all"})
    on = {i for i, e in config["keychain_sources"].items() if e["read"]}
    assert "anthropic.chrome" in on and "anthropic.edge" in on
    assert not any(i.endswith(".chromium") for i in on)


def test_ticking_a_browser_again_does_not_turn_anything_on(settings):
    server, config = settings
    send(server, "POST", "/sources", {"action": "set_browsers", "value": {"edge": False}})
    send(server, "POST", "/sources", {"action": "set_browsers", "value": {"edge": True}})
    assert config["keychain_browsers"]["edge"] is True
    assert not any(e["read"] for e in config.get("keychain_sources", {}).values())


def test_authorize_all_hands_the_platform_sources_to_the_controller(settings, monkeypatch):
    import sys

    from scripts.sidecar_pkg import source_setup

    server, _config = settings
    calls = []
    monkeypatch.setattr(
        source_setup.SETUP,
        "start_all",
        lambda ids: calls.append(list(ids)) or {"active": True, "total": 2},
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    status, raw = send(server, "POST", "/sources", {"action": "authorize_all"})
    assert status == 200 and json.loads(raw) == {"ok": True, "run": {"active": True, "total": 2}}
    assert calls and "anthropic.claude-code" in calls[0] and "anthropic.chrome" in calls[0]


def test_authorize_cancel_asks_the_controller_to_stop(settings, monkeypatch):
    from unittest.mock import Mock

    from scripts.sidecar_pkg import source_setup

    server, _config = settings
    cancel = Mock()
    monkeypatch.setattr(source_setup.SETUP, "cancel", cancel)
    assert send(server, "POST", "/sources", {"action": "authorize_cancel"})[0] == 200
    cancel.assert_called_once_with()


def test_authorize_actions_need_the_same_origin(settings):
    server, _config = settings
    assert send(server, "POST", "/sources", origin=False)[0] == 403


def test_page_has_one_compact_section_and_no_per_row_buttons(settings):
    server, _config = settings
    status, raw = send(server, "GET", "/")
    html = raw.decode()
    assert status == 200
    # Hidden until the report says this is a Mac: nothing to show elsewhere.
    assert (
        '<section class="card" id="sources-card" aria-labelledby="sources-heading" hidden>' in html
    )
    assert "authorize_all" in html and "set_browsers" in html and "set_sources" in html
    # The per-row Authorize and Dismiss buttons are replaced by the batch bar.
    assert "Dismiss new scraper notice" not in html
    assert "'Authorize'" not in html
    assert "Authorize ' + todo.length + ' enabled" in html


def test_page_script_parses(settings):
    import re
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    server, _config = settings
    html = send(server, "GET", "/")[1].decode()
    script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
