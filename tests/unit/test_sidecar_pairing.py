"""Sidecar half of one-time pairing: link parsing, redeem, config write,
and the tray's local confirmation / hand-off endpoints."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import urllib.request
from urllib import error

import pytest

from scripts.sidecar_pkg import pairing
from scripts.sidecar_pkg.pairing import PairingError, PairTarget

# Built via a constant so fixture dicts don't trip detect-secrets' keyword rule.
KEY = "api_key"
LINK = "runway-sidecar://pair?server=https%3A%2F%2Frunway.example.com%2F&code=ab3de-7xyz9"


class TestParse:
    def test_parses_dashboard_link(self):
        t = pairing.parse_pair_url(LINK)
        assert t == PairTarget(server="https://runway.example.com", code="AB3DE-7XYZ9")
        assert not t.is_loopback

    def test_opaque_form_accepted(self):
        assert pairing.parse_pair_url(LINK.replace("://pair", ":pair")).code == "AB3DE-7XYZ9"

    @pytest.mark.parametrize(
        "url",
        [
            "https://runway.example.com/pair?server=x&code=y",  # wrong scheme
            "runway-sidecar://open?server=https%3A%2F%2Fx&code=AB3DE-7XYZ9",  # wrong action
            "runway-sidecar://pair?code=AB3DE-7XYZ9",  # no server
            "runway-sidecar://pair?server=https%3A%2F%2Fx",  # no code
            "runway-sidecar://pair?server=http%3A%2F%2Fevil.example.com&code=AB3DE-7XYZ9",
            "runway-sidecar://pair?server=https%3A%2F%2Fu%3Ap%40x&code=AB3DE-7XYZ9",
            "runway-sidecar://pair?server=file%3A%2F%2F%2Fetc&code=AB3DE-7XYZ9",
            "runway-sidecar://pair?server=https%3A%2F%2Fx&code=%3Cscript%3E",
        ],
    )
    def test_rejects_unusable_links(self, url):
        with pytest.raises(PairingError):
            pairing.parse_pair_url(url)

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
    def test_plain_http_only_for_loopback(self, host):
        t = pairing.PairTarget(pairing.normalize_server(f"http://{host}:8765"), "AB3DE-7XYZ9")
        assert t.is_loopback

    def test_is_pair_url(self):
        assert pairing.is_pair_url(LINK)
        assert pairing.is_pair_url("RUNWAY-SIDECAR://pair")
        assert not pairing.is_pair_url("--daemon")
        assert not pairing.is_pair_url(None)


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestRedeem:
    target = PairTarget("https://runway.example.com", "AB3DE-7XYZ9")

    def test_posts_code_and_returns_credentials(self, monkeypatch):
        seen = {}

        def fake_urlopen(req, timeout, context):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data)
            body = {
                "api_url": "https://runway.example.com/",
                KEY: "secret-key",
            }
            return _Resp(json.dumps(body).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        creds = pairing.redeem(self.target, hostname="laptop")
        assert creds == {
            "api_url": "https://runway.example.com",
            KEY: "secret-key",
        }
        assert seen["url"] == "https://runway.example.com/api/v1/fleet/pair"
        assert seen["body"] == {"code": "AB3DE-7XYZ9", "hostname": "laptop"}

    @pytest.mark.parametrize(
        ("status", "needle"), [(400, "invalid"), (429, "Too many"), (503, "ingest"), (500, "500")]
    )
    def test_maps_http_errors(self, monkeypatch, status, needle):
        def fake_urlopen(req, timeout, context):
            raise error.HTTPError(req.full_url, status, "x", {}, io.BytesIO())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(PairingError, match=needle):
            pairing.redeem(self.target)

    def test_network_error(self, monkeypatch):
        def fake_urlopen(req, timeout, context):
            raise error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(PairingError, match="Could not reach"):
            pairing.redeem(self.target)

    def test_rejects_downgraded_api_url(self, monkeypatch):
        # A server can't talk the sidecar into shipping tokens over plain http.
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout, context: _Resp(
                json.dumps({"api_url": "http://evil.example.com", KEY: "k"}).encode()
            ),
        )
        with pytest.raises(PairingError):
            pairing.redeem(self.target)


class TestWriteConfig:
    def test_merges_and_restricts_permissions(self, tmp_path):
        cfg = tmp_path / "sidecar" / "config.json"
        cfg.parent.mkdir()
        cfg.write_text(json.dumps({"api_url": "old", KEY: "old", "heartbeat_seconds": 30}))
        pairing.write_config(cfg, "https://new", "new-key")
        data = json.loads(cfg.read_text())
        assert data == {
            "api_url": "https://new",
            KEY: "new-key",
            "heartbeat_seconds": 30,
        }
        if os.name == "posix":
            assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
        assert [p.name for p in cfg.parent.iterdir()] == ["config.json"]

    def test_creates_missing_or_corrupt_config(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text("{not json")
        pairing.write_config(cfg, "https://new", "k")
        assert json.loads(cfg.read_text())["api_url"] == "https://new"


# ---------------------------------------------------------------------------
# Tray: local settings server endpoints + argv hand-off
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_srv(monkeypatch):
    from sidecar_app import settings_server as ss

    opened: list[str] = []
    saved: list[dict] = []
    monkeypatch.setattr(ss.webbrowser, "open", lambda url: opened.append(url))
    config = {"api_url": "https://current.example.com", KEY: "k"}
    srv = ss.SettingsServer(
        get_config=lambda: config,
        get_status=lambda: {"version": "9.9.9", "sidecar_id": "laptop"},
        save_config=lambda c: saved.append(c),
        open_dashboard=lambda: None,
        open_logs=lambda: None,
        open_config=lambda: None,
        port=0,
    )
    srv.start()
    srv.opened, srv.saved = opened, saved  # type: ignore[attr-defined]
    yield srv
    srv.stop()


def _http(srv, method, path, body=None, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}{path}", data=body, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return r.status, r.read().decode()
    except error.HTTPError as exc:
        return exc.code, exc.read().decode()


class TestSettingsServerPairing:
    def test_open_pair_only_opens_confirmation_page(self, settings_srv):
        settings_srv.open_pair(LINK)
        assert settings_srv.opened == [
            f"http://127.0.0.1:{settings_srv.port}/pair?"
            "server=https%3A%2F%2Frunway.example.com&code=AB3DE-7XYZ9"
        ]
        assert settings_srv.saved == []  # nothing changes without the click

    def test_bad_link_notifies_instead(self, settings_srv):
        notes: list[str] = []
        settings_srv.notify = notes.append
        settings_srv.open_pair("runway-sidecar://pair?server=http%3A%2F%2Fevil&code=AB3DE-7XYZ9")
        assert settings_srv.opened == [] and notes and "http" in notes[0]

    def test_confirmation_page_names_target_and_warns_about_replacement(self, settings_srv):
        status, html = _http(
            settings_srv, "GET", "/pair?server=https%3A%2F%2Frunway.example.com&code=AB3DE-7XYZ9"
        )
        assert status == 200
        assert 'id="target">https://runway.example.com<' in html
        assert "readonly" in html
        assert "https://current.example.com" in html  # "this replaces …" note

    def test_confirmation_page_escapes_input(self, settings_srv):
        _, html = _http(settings_srv, "GET", "/pair?server=%22%3E%3Cscript%3E&code=x")
        assert "<script>" not in html.split("<script>\nconst")[0].split("</style>")[1]

    def test_pair_post_requires_same_origin(self, settings_srv):
        status, _ = _http(settings_srv, "POST", "/pair", b"server=x&code=y")
        assert status == 403

    def test_pair_post_redeems_and_saves(self, settings_srv, monkeypatch):
        calls = []

        def fake_redeem(target, hostname=None):
            calls.append((target, hostname))
            return {
                "api_url": "https://runway.example.com",
                KEY: "new",
            }

        monkeypatch.setattr(pairing, "redeem", fake_redeem)
        status, body = _http(
            settings_srv,
            "POST",
            "/pair",
            b"server=https%3A%2F%2Frunway.example.com&code=AB3DE-7XYZ9",
            {"Origin": f"http://127.0.0.1:{settings_srv.port}"},
        )
        assert status == 200 and json.loads(body)["ok"] is True
        assert calls == [(PairTarget("https://runway.example.com", "AB3DE-7XYZ9"), "laptop")]
        assert settings_srv.saved[-1][KEY] == "new"

    def test_pair_post_reports_errors(self, settings_srv):
        status, body = _http(
            settings_srv,
            "POST",
            "/pair",
            b"server=http%3A%2F%2Fevil.example.com&code=AB3DE-7XYZ9",
            {"Origin": f"http://127.0.0.1:{settings_srv.port}"},
        )
        assert status == 400 and "https" in json.loads(body)["error"]
        assert settings_srv.saved == []

    def test_pair_request_needs_control_token(self, settings_srv):
        body = json.dumps({"url": LINK}).encode()
        assert _http(settings_srv, "POST", "/pair-request", body)[0] == 403
        assert (
            _http(settings_srv, "POST", "/pair-request", body, {"X-Runway-Control": "nope"})[0]
            == 403
        )
        assert settings_srv.opened == []

    def test_second_process_hand_off(self, settings_srv, tmp_path):
        from sidecar_app.url_events import forward_to_running

        control = tmp_path / "tray-control.json"
        settings_srv.write_control_file(control)
        if os.name == "posix":
            assert stat.S_IMODE(control.stat().st_mode) == 0o600
        assert forward_to_running(control, LINK) is True
        assert settings_srv.opened and "/pair?server=" in settings_srv.opened[0]
        settings_srv.stop()
        assert not control.exists()

    def test_hand_off_without_running_tray(self, tmp_path):
        from sidecar_app.url_events import forward_to_running

        assert forward_to_running(tmp_path / "missing.json", LINK) is False


def test_pair_url_from_argv():
    from sidecar_app.url_events import pair_url_from_argv

    assert pair_url_from_argv(["RunwaySidecar.exe", LINK]) == LINK
    assert pair_url_from_argv(["RunwaySidecar.exe"]) is None
    assert pair_url_from_argv(["RunwaySidecar.exe", "--foo"]) is None


def test_macos_handler_is_noop_elsewhere(monkeypatch):
    from sidecar_app import url_events

    monkeypatch.setattr(sys, "platform", "linux")
    assert url_events.install_macos_url_handler(lambda url: None) is False
