"""Local source settings endpoints must be inert and keep grants local."""

import http.client
import json
import threading

import pytest

from sidecar_app.settings_server import _SettingsServer


@pytest.fixture
def settings(monkeypatch):
    from scripts.sidecar_pkg import source_settings

    monkeypatch.setattr(
        source_settings,
        "installed_browsers",
        lambda: {"chrome": True, "edge": True, "chromium": True},
    )
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
    assert config["keychain_browsers"] == {"edge": False}
    edge = [i for i in config["keychain_sources"] if i.endswith(".edge")]
    assert edge and all(config["keychain_sources"][i]["read"] is False for i in edge)
    assert set(edge) <= set(config["keychain_withdrawals"])
    chrome = [i for i in config["keychain_sources"] if i.endswith(".chrome")]
    assert all(config["keychain_sources"][i]["read"] is True for i in chrome)
    assert config["keychain_sources"]["anthropic.claude-code"]["read"] is True
    assert _report(server)["browsers"] == {"chrome": True, "edge": False, "chromium": True}


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
    # One poll timer at most while a run is active, and ids on the controls so focus survives a refresh.
    assert "kcPoll === null" in html
    for marker in ("'source-' + s.id", "'browser-' + key", "'service-' + service"):
        assert marker in html


def test_page_script_parses(settings):
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    server, _config = settings
    html = send(server, "GET", "/")[1].decode()
    # The page's own script is the last inline one; split on its tags rather than pattern-match HTML.
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _patch_darwin_dirs(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from scripts.sidecar_pkg import source_settings

    home = tmp_path / "home"
    apps = tmp_path / "Applications"
    home.mkdir(parents=True, exist_ok=True)
    apps.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(source_settings, "sys", SimpleNamespace(platform="darwin", frozen=False))
    monkeypatch.setattr(source_settings.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(source_settings, "_APPLICATIONS_DIR", apps)
    return home, apps, source_settings


def test_installed_browsers_and_default_selection_with_fake_dirs(tmp_path, monkeypatch):
    from scripts.sidecar_pkg.source_catalog import BROWSERS

    _home, apps, source_settings = _patch_darwin_dirs(monkeypatch, tmp_path)
    assert set(BROWSERS) == set(source_settings._BROWSER_INSTALL_TARGETS)
    (apps / "Google Chrome.app").mkdir()

    assert source_settings.installed_browsers() == {
        "chrome": True,
        "edge": False,
        "chromium": False,
    }
    assert source_settings.browsers({}) == {
        "chrome": True,
        "edge": False,
        "chromium": False,
    }
    assert source_settings.browsers({"keychain_browsers": {"edge": True}}) == {
        "chrome": True,
        "edge": True,
        "chromium": False,
    }
    assert source_settings.browsers({"keychain_browsers": {"chrome": False}}) == {
        "chrome": False,
        "edge": False,
        "chromium": False,
    }
    rep = source_settings.report({})
    assert rep["installed"] == {"chrome": True, "edge": False, "chromium": False}
    assert rep["browsers"] == {"chrome": True, "edge": False, "chromium": False}


def test_installed_browsers_detects_user_dirs_and_is_not_cached(tmp_path, monkeypatch):
    home, apps, source_settings = _patch_darwin_dirs(monkeypatch, tmp_path)
    (apps / "Google Chrome.app").mkdir()

    assert source_settings.report({})["installed"] == {
        "chrome": True,
        "edge": False,
        "chromium": False,
    }
    # Toggling one browser must not persist derived False for uninstalled ones.
    updated = source_settings.update({}, {"action": "set_browsers", "value": {"chrome": False}})
    assert updated["keychain_browsers"] == {"chrome": False}

    (home / "Applications" / "Microsoft Edge.app").mkdir(parents=True)
    (home / "Library" / "Application Support" / "Chromium").mkdir(parents=True)

    rep = source_settings.report(updated)
    assert rep["installed"] == {"chrome": True, "edge": True, "chromium": True}
    assert rep["browsers"] == {"chrome": False, "edge": True, "chromium": True}


def test_non_darwin_browsers_default_true_and_report_off(monkeypatch):
    from types import SimpleNamespace

    from scripts.sidecar_pkg import source_settings

    monkeypatch.setattr(source_settings, "sys", SimpleNamespace(platform="linux", frozen=False))
    assert source_settings.browsers({}) == {"chrome": True, "edge": True, "chromium": True}
    rows = {row["id"]: row for row in source_settings.report({})["sources"]}
    assert rows["kimi_coding.chrome"]["state"] == "Off"


def test_enable_all_and_set_sources_skip_uninstalled_browsers_and_never_action_required(
    tmp_path, monkeypatch
):
    _home, apps, source_settings = _patch_darwin_dirs(monkeypatch, tmp_path)
    (apps / "Google Chrome.app").mkdir()

    from scripts.sidecar_pkg import keychain_client, source_protocol

    client = keychain_client.KeychainClient(command=["synthetic-never-started"])
    client.block_access("storage:Microsoft Edge Safe Storage", "denied")
    monkeypatch.setattr(keychain_client, "_CLIENT", client)

    updated = source_settings.update({}, {"action": "enable_all"})
    assert "anthropic.chrome" in updated["keychain_sources"]
    assert "anthropic.edge" not in updated["keychain_sources"]
    assert "anthropic.chromium" not in updated["keychain_sources"]

    # set_sources also skips uninstalled browsers when enabling.
    set_updated = source_settings.update(
        {},
        {
            "action": "set_sources",
            "source_ids": ["anthropic.chrome", "anthropic.edge"],
            "value": True,
        },
    )
    assert "anthropic.chrome" in set_updated["keychain_sources"]
    assert "anthropic.edge" not in set_updated["keychain_sources"]

    rows = {row["id"]: row for row in source_settings.report(updated)["sources"]}
    assert rows["anthropic.chrome"]["read"] is True
    assert rows["anthropic.edge"]["read"] is False
    assert rows["anthropic.edge"]["state"] == "Not installed"
    assert rows["anthropic.edge"]["new"] is False
    assert rows["anthropic.chromium"]["state"] == "Not installed"

    proto_client = source_protocol.SourceClient(
        {"device_id": "device-" + "a" * 32, "api_url": "https://runway.example"},
        tmp_path / "state.json",
    )
    payload_rows = {
        row["source_id"]: row
        for row in proto_client.payload(updated, source_settings.report(updated))["sources"]
    }
    assert payload_rows["anthropic.edge"]["state"] == "Not installed"
    assert payload_rows["anthropic.edge"]["read"] is False
    assert payload_rows["anthropic.chromium"]["state"] == "Not installed"

    # Explicit override in keychain_browsers still allows enable_all to select Edge.
    with_edge = source_settings.update(
        {"keychain_browsers": {"edge": True}}, {"action": "enable_all"}
    )
    assert "anthropic.edge" in with_edge["keychain_sources"]


def test_settings_page_labels_uninstalled_browser_chips_and_excludes_from_counts(
    tmp_path, monkeypatch
):
    import shutil
    import subprocess

    from sidecar_app.settings_server import _CSS, _HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    html = _HTML.substitute(
        css=_CSS,
        api_url="https://runway.example",
        version="0.0.0",
        sidecar_id="test",
    )
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]

    _home, apps, source_settings = _patch_darwin_dirs(monkeypatch, tmp_path)
    (apps / "Google Chrome.app").mkdir()
    updated = source_settings.update({}, {"action": "enable_all"})
    report = source_settings.report(updated)
    expected_live = [
        s
        for s in report["sources"]
        if s["compatible"] and (s["group"] == "app" or s["group"] == "chrome")
    ]
    assert len(expected_live) > 0

    probe = """
const vm = require('vm');
const fs = require('fs');
const report = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const script = fs.readFileSync(process.argv[2], 'utf8');
const byId = new Map();
function makeEl(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    className: '',
    textContent: '',
    title: '',
    hidden: false,
    disabled: false,
    checked: false,
    _id: '',
    style: {},
    setAttribute() {},
    append(...nodes) {
      for (const n of nodes) {
        if (typeof n === 'string') {
          this.children.push({ textContent: n, children: [] });
        } else if (n) {
          this.children.push(n);
        }
      }
    },
    replaceChildren(...nodes) {
      this.children = [];
      this.append(...nodes);
    },
    get firstChild() { return this.children[0] || null; },
  };
  Object.defineProperty(el, 'id', {
    get() { return this._id; },
    set(v) { this._id = v; if (v) byId.set(v, el); },
  });
  return el;
}
function collectText(node) {
  if (!node) return '';
  return (node.textContent || '') + (node.children || []).map(collectText).join('');
}
function findAll(node, pred, out = []) {
  if (pred(node)) out.push(node);
  for (const c of node.children || []) findAll(c, pred, out);
  return out;
}
const card = makeEl('section'); card.id = 'sources-card';
const body = makeEl('div'); body.id = 'sources-body';
const ctx = {
  document: {
    getElementById: (id) => byId.get(id) || makeEl('div'),
    createElement: (tag) => makeEl(tag),
    createTextNode: (t) => ({ textContent: String(t), children: [] }),
  },
  window: { addEventListener() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0,
  URLSearchParams, Map, Set, Math, JSON,
};
vm.runInNewContext(script, ctx);
ctx.renderSources(report);
const labels = findAll(body, n => n.tagName === 'LABEL' && (n.children || []).some(c => c._id && c._id.startsWith('browser-')));
const chips = {};
for (const l of labels) {
  const box = l.children.find(c => c._id && c._id.startsWith('browser-'));
  chips[box._id] = { disabled: box.disabled, checked: box.checked, text: collectText(l) };
}
const summary = findAll(body, n => n.className === 'kc-summary').map(collectText)[0] || '';
const authBtn = byId.get('authorize-all');
console.log(JSON.stringify({ chips, summary, authText: authBtn ? collectText(authBtn) : '' }));
"""
    # Pass the report and page script as files: both together exceed the Windows
    # command-line limit (WinError 206) when given as argv.
    report_file = tmp_path / "report.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    script_file = tmp_path / "page.js"
    script_file.write_text(script, encoding="utf-8")
    res = subprocess.run(
        [node, "-e", probe, str(report_file), str(script_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    rendered = json.loads(res.stdout)
    assert rendered["chips"]["browser-chrome"]["disabled"] is False
    assert rendered["chips"]["browser-chrome"]["checked"] is True
    assert "Not installed" not in rendered["chips"]["browser-chrome"]["text"]
    for key in ("browser-edge", "browser-chromium"):
        assert rendered["chips"][key]["disabled"] is True
        assert rendered["chips"][key]["checked"] is False
        assert "Not installed" in rendered["chips"][key]["text"]
    expected_items = len({s["item"] for s in expected_live})
    assert f"{len(expected_live)} of {len(expected_live)} on" in rendered["summary"]
    assert f"{len(expected_live)} need authorization" in rendered["summary"]
    assert (
        f"Authorize {len(expected_live)} enabled · {expected_items} Keychain item"
        in rendered["authText"]
    )


def test_safari_row_and_first_open_notice_lifecycle(settings, monkeypatch):
    import sys

    from scripts.sidecar_pkg import safari_cookies

    server, config = settings
    state = ["full_disk_access_missing"]
    monkeypatch.setattr(sys, "platform", "darwin")
    safari_cookies._sidecar_config_path().unlink(missing_ok=True)
    monkeypatch.setattr(safari_cookies, "_evaluate_access", lambda home=None: state[0])

    # First open with missing Full Disk Access shows the notice and the Safari row.
    first = _report(server)["safari"]
    assert first == {
        "id": "safari",
        "label": "Safari",
        "probe_state": "full_disk_access_missing",
        "state": "Full Disk Access needed",
        "notice": True,
    }

    # Dismissing remembers the state in config and hides the notice while unchanged.
    assert send(server, "POST", "/sources", {"action": "dismiss_safari_notice"})[0] == 200
    assert config["safari_fda_notice_dismissed_state"] == "full_disk_access_missing"
    assert config["safari_fda_last_state"] == "full_disk_access_missing"
    assert _report(server)["safari"]["notice"] is False

    # Transitioning to readable updates the row and keeps the notice hidden.
    state[0] = "readable"
    readable = _report(server)["safari"]
    assert readable["state"] == "Readable"
    assert readable["notice"] is False

    # Transitioning back to missing re-shows the notice.
    state[0] = "full_disk_access_missing"
    assert _report(server)["safari"]["notice"] is True

    # Dismiss again, then flip missing -> readable -> missing via background probe_access
    # without opening /sources in between: the persisted state re-arms the notice.
    assert send(server, "POST", "/sources", {"action": "dismiss_safari_notice"})[0] == 200
    assert _report(server)["safari"]["notice"] is False
    state[0] = "readable"
    assert safari_cookies.probe_access() == "readable"
    state[0] = "full_disk_access_missing"
    assert safari_cookies.probe_access() == "full_disk_access_missing"
    assert _report(server)["safari"]["notice"] is True

    # Also test when the caller's in-memory config stays stale while probe_access
    # flips missing -> readable -> missing on disk, and when another /sources POST
    # or watch_config reload happens in between.
    assert send(server, "POST", "/sources", {"action": "dismiss_safari_notice"})[0] == 200
    stale_snapshot = dict(config)
    state[0] = "readable"
    assert safari_cookies.probe_access() == "readable"
    # Even if in-memory config is stale when a /sources POST happens between grant and loss,
    # update() syncs the on-disk Safari state before saving so the transition is not erased.
    config.clear()
    config.update(stale_snapshot)
    assert (
        send(server, "POST", "/sources", {"action": "enable", "source_id": "anthropic.chrome"})[0]
        == 200
    )
    assert config["safari_fda_last_state"] == "readable"
    assert config["safari_fda_notice_dismissed_state"] == ""
    state[0] = "full_disk_access_missing"
    assert safari_cookies.probe_access() == "full_disk_access_missing"
    assert _report(server)["safari"]["notice"] is True

    # Dismissing again after a disk reload stays dismissed on subsequent GET polls,
    # even if a legacy safari_fda_notice_rearmed flag was present in live/disk config.
    loaded_from_disk = json.loads(safari_cookies._sidecar_config_path().read_text(encoding="utf-8"))
    loaded_from_disk["safari_fda_notice_rearmed"] = True
    safari_cookies._sidecar_config_path().write_text(json.dumps(loaded_from_disk), encoding="utf-8")
    config.clear()
    config.update(loaded_from_disk)
    assert _report(server)["safari"]["notice"] is True
    assert send(server, "POST", "/sources", {"action": "dismiss_safari_notice"})[0] == 200
    assert "safari_fda_notice_rearmed" not in config
    assert "safari_fda_notice_rearmed" not in json.loads(
        safari_cookies._sidecar_config_path().read_text(encoding="utf-8")
    )
    assert _report(server)["safari"]["notice"] is False
    assert _report(server)["safari"]["notice"] is False

    # Not-used state maps cleanly too.
    state[0] = "not_used"
    not_used = _report(server)["safari"]
    assert not_used["state"] == "Safari not used"
    assert not_used["notice"] is False


def test_safari_notice_rearms_when_grant_observed_only_via_get_sources(settings, monkeypatch):
    import sys

    from scripts.sidecar_pkg import safari_cookies
    from sidecar_app.config import save_local_config

    server, config = settings
    state = ["full_disk_access_missing"]
    monkeypatch.setattr(sys, "platform", "darwin")
    config_path = safari_cookies._sidecar_config_path()
    config_path.unlink(missing_ok=True)
    monkeypatch.setattr(safari_cookies, "_evaluate_access", lambda home=None: state[0])

    def _save_config(value: dict) -> None:
        config.update(value)
        save_local_config(config_path, config)

    monkeypatch.setattr(server, "save_config", _save_config)

    # 1. Dismiss while Full Disk Access is missing.
    assert _report(server)["safari"]["notice"] is True
    assert send(server, "POST", "/sources", {"action": "dismiss_safari_notice"})[0] == 200
    assert _report(server)["safari"]["notice"] is False

    # 2. Grant observed only via GET /sources persists re-arm state to disk,
    #    so an intervening POST /sources does not erase it before access is lost.
    state[0] = "readable"
    readable = _report(server)["safari"]
    assert readable["state"] == "Readable"
    assert readable["notice"] is False
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["safari_fda_last_state"] == "readable"
    assert saved["safari_fda_notice_dismissed_state"] == ""

    assert (
        send(server, "POST", "/sources", {"action": "enable", "source_id": "anthropic.chrome"})[0]
        == 200
    )
    after_post = json.loads(config_path.read_text(encoding="utf-8"))
    assert after_post["safari_fda_last_state"] == "readable"
    assert after_post["safari_fda_notice_dismissed_state"] == ""
    state[0] = "full_disk_access_missing"
    assert _report(server)["safari"]["notice"] is True

    # 3. Repeat with a simulated sidecar restart (reload config from disk) between grant and loss.
    assert send(server, "POST", "/sources", {"action": "dismiss_safari_notice"})[0] == 200
    assert _report(server)["safari"]["notice"] is False
    state[0] = "readable"
    assert _report(server)["safari"]["state"] == "Readable"

    reloaded = json.loads(config_path.read_text(encoding="utf-8"))
    config.clear()
    config.update(reloaded)

    state[0] = "full_disk_access_missing"
    assert _report(server)["safari"]["notice"] is True


def test_full_disk_access_action_and_page_markers(settings, monkeypatch):
    server, _config = settings
    called = []
    monkeypatch.setattr(server, "open_full_disk_access", lambda: called.append(True))

    assert send(server, "POST", "/action/full_disk_access", origin=False)[0] == 403
    assert send(server, "POST", "/action/full_disk_access")[0] == 200
    assert called == [True]

    html = send(server, "GET", "/")[1].decode()
    for marker in (
        "safari-row",
        "safari-fda-notice",
        "dismiss_safari_notice",
        "/action/full_disk_access",
        "window.addEventListener('focus'",
    ):
        assert marker in html


def test_settings_panel_banner_distinguishes_sync_states(tmp_path, monkeypatch):
    import shutil
    import subprocess

    from sidecar_app.settings_server import _CSS, _HTML

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    html = _HTML.substitute(
        css=_CSS,
        api_url="https://runway.example",
        version="0.0.0",
        sidecar_id="test",
    )
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    _home, _apps, source_settings = _patch_darwin_dirs(monkeypatch, tmp_path)
    base_report = source_settings.report({})

    probe = """
const vm = require('vm');
const fs = require('fs');
const baseReport = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const script = fs.readFileSync(process.argv[2], 'utf8');
const byId = new Map();
function makeEl(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    className: '',
    textContent: '',
    title: '',
    hidden: false,
    disabled: false,
    checked: false,
    _id: '',
    style: {},
    setAttribute() {},
    append(...nodes) {
      for (const n of nodes) {
        if (typeof n === 'string') this.children.push({ textContent: n, children: [] });
        else if (n) this.children.push(n);
      }
    },
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); },
    get firstChild() { return this.children[0] || null; },
  };
  Object.defineProperty(el, 'id', {
    get() { return this._id; },
    set(v) { this._id = v; if (v) byId.set(v, el); },
  });
  return el;
}
function collectText(node) {
  if (!node) return '';
  return (node.textContent || '') + (node.children || []).map(collectText).join('');
}
const card = makeEl('section'); card.id = 'sources-card';
const body = makeEl('div'); body.id = 'sources-body';
const ctx = {
  document: {
    getElementById: id => byId.get(id) || makeEl('div'),
    createElement: makeEl,
    createTextNode: text => ({ textContent: String(text), children: [] }),
    activeElement: null,
  },
  window: { addEventListener() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  setInterval: () => 0,
  setTimeout: () => 0,
  clearTimeout: () => {},
  URLSearchParams,
  Map,
  Set,
  Math,
  JSON,
};
vm.runInNewContext(script, ctx);

function bannerFor(patch) {
  ctx.renderSources(Object.assign({}, baseReport, patch));
  const banner = body.children.find(c => (c.className || '').includes('kc-banner'));
  return banner ? collectText(banner) : '';
}

console.log(JSON.stringify({
  not_synced_yet: bannerFor({ sync_state: 'not_synced_yet', sync_reason: null, forwarding_supported: false }),
  redirect: bannerFor({ sync_state: 'sync_failed', sync_reason: 'redirect', forwarding_supported: false }),
  http_500: bannerFor({ sync_state: 'sync_failed', sync_reason: 'http_500', forwarding_supported: false }),
  network: bannerFor({ sync_state: 'sync_failed', sync_reason: 'network', forwarding_supported: false }),
  invalid_reply: bannerFor({ sync_state: 'sync_failed', sync_reason: 'invalid_reply', forwarding_supported: false }),
  local_state: bannerFor({ sync_state: 'sync_failed', sync_reason: 'local_state', forwarding_supported: false }),
  re_pair_required: bannerFor({ sync_state: 're_pair_required', sync_reason: 'http_401', re_pair_required: true, forwarding_supported: false }),
  server_unsupported: bannerFor({ sync_state: 'server_unsupported', sync_reason: 'http_404', forwarding_supported: false }),
}));
"""
    report_path = tmp_path / "report.json"
    script_path = tmp_path / "page.js"
    report_path.write_text(json.dumps(base_report), encoding="utf-8")
    script_path.write_text(script, encoding="utf-8")

    proc = subprocess.run(
        [node, "-e", probe, str(report_path), str(script_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    banners = json.loads(proc.stdout)
    assert "Waiting for the first sync with the server..." in banners["not_synced_yet"]
    for key in (
        "not_synced_yet",
        "redirect",
        "http_500",
        "network",
        "invalid_reply",
        "local_state",
        "re_pair_required",
    ):
        assert "does not support" not in banners[key]
    for reason in ("redirect", "http_500", "network", "invalid_reply", "local_state"):
        assert reason in banners[reason]
    assert "does not support" in banners["server_unsupported"]


def test_get_sources_triggers_or_awaits_sync_once(tmp_path, monkeypatch):
    from scripts.sidecar_pkg import local_paths, source_protocol, transport

    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(local_paths, "get_sidecar_dir", lambda: tmp_path)

    config = {
        "api_url": "https://runway.example",
        "device_api_url": "https://runway.example",
        "device_id": "a" * 32,
        "device_key": "synthetic-device-key",
    }
    calls = []
    started = threading.Event()
    release = threading.Event()

    class _FakeResponse:
        def __init__(self, url: str):
            self._url = url

        def geturl(self) -> str:
            return self._url

        def read(self, _limit: int = -1) -> bytes:
            return json.dumps(
                {
                    "protocol": 1,
                    "accepted": ["anthropic.claude-code"],
                    "commands": [],
                    "withdrawals": [],
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(req, **_kw):
        calls.append(req.full_url)
        started.set()
        release.wait(timeout=5)
        return _FakeResponse(req.full_url)

    monkeypatch.setattr(transport, "urlopen", fake_urlopen)

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
    try:
        # 1. Producing the report triggers a sync once; subsequent polls reuse it.
        release.set()
        status, raw = send(server, "GET", "/sources")
        assert status == 200
        rep = json.loads(raw)
        assert rep["sync_state"] == "ok"
        assert rep["forwarding_supported"] is True
        assert len(calls) == 1

        status2, raw2 = send(server, "GET", "/sources")
        assert status2 == 200
        assert json.loads(raw2)["sync_state"] == "ok"
        assert len(calls) == 1

        # 2. If a sync is already in flight, GET /sources awaits it without starting another.
        calls.clear()
        started.clear()
        release.clear()
        source_protocol.CLIENT = None
        bg = threading.Thread(
            target=source_protocol.sync,
            args=(
                config,
                tmp_path / "source-control.json",
                lambda cfg: __import__(
                    "scripts.sidecar_pkg.source_settings", fromlist=["report"]
                ).report(cfg),
            ),
        )
        bg.start()
        assert started.wait(timeout=5)

        result_holder = []
        getter = threading.Thread(
            target=lambda: result_holder.append(send(server, "GET", "/sources"))
        )
        getter.start()
        release.set()
        bg.join(timeout=5)
        getter.join(timeout=5)
        assert len(calls) == 1
        assert result_holder[0][0] == 200
        assert json.loads(result_holder[0][1])["sync_state"] == "ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
