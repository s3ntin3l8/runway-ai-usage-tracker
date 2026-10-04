"""XaiRenewer: refresh the xAI login and write it back to the CLI's auth file."""

import base64
import json
import logging
import os
import time
from pathlib import Path

import pytest

from app.services import token_refresher
from scripts.sidecar_pkg import keep_alive, xai_renewer
from scripts.sidecar_pkg.xai_renewer import XaiRenewer


def _jwt(exp: float) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'exp': int(exp)})}.sig"


def _opencode(path: Path, exp: float, refresh: str = "r-old") -> Path:
    path.write_text(
        json.dumps(
            {
                "openrouter": {"type": "api", "key": "keep-me"},
                "xai": {
                    "type": "oauth",
                    "access": _jwt(exp),
                    "refresh": refresh,
                    "expires": int(exp * 1000),
                    "extra": "kept",
                },
            }
        )
    )
    path.chmod(0o600)
    return path


def _grok(path: Path, exp: float) -> Path:
    path.write_text(
        json.dumps(
            {
                "https://auth.x.ai::client": {
                    "key": _jwt(exp),
                    "refresh_token": "g-old",
                    "first_name": "A",
                }
            }
        )
    )
    return path


def _respond(monkeypatch, data=None, error=None):
    calls: list[str] = []

    def fake(refresh_token):
        calls.append(refresh_token)
        if error:
            raise error
        return data

    monkeypatch.setattr(xai_renewer, "request_refresh", fake)
    return calls


def test_constants_match_the_server_refresher():
    assert token_refresher._REFRESH_ENDPOINTS["xai"] == xai_renewer.TOKEN_ENDPOINT
    assert token_refresher._PROVIDER_CLIENT_IDS["xai"] == xai_renewer.CLIENT_ID


class TestDue:
    def test_not_due_while_valid(self, tmp_path):
        f = _opencode(tmp_path / "a.json", time.time() + 3600)
        assert XaiRenewer([(f, "opencode")]).due() is False

    def test_due_inside_lead_window(self, tmp_path):
        f = _opencode(tmp_path / "a.json", time.time() + 60)
        assert XaiRenewer([(f, "opencode")]).due() is True

    def test_not_due_without_file_or_refresh_token(self, tmp_path):
        assert XaiRenewer([(tmp_path / "missing.json", "opencode")]).due() is False
        f = tmp_path / "a.json"
        f.write_text(json.dumps({"xai": {"access": _jwt(1), "refresh": ""}}))
        assert XaiRenewer([(f, "opencode")]).due() is False

    def test_jwt_exp_beats_stale_expires_field(self, tmp_path):
        f = _opencode(tmp_path / "a.json", time.time() - 10)
        data = json.loads(f.read_text())
        data["xai"]["expires"] = int((time.time() + 9999) * 1000)
        f.write_text(json.dumps(data))
        assert XaiRenewer([(f, "opencode")]).due() is True


class TestRenew:
    def test_rewrites_only_the_xai_entry(self, tmp_path, monkeypatch):
        f = _opencode(tmp_path / "a.json", time.time() - 10)
        new_exp = time.time() + 6 * 3600
        calls = _respond(monkeypatch, {"access_token": _jwt(new_exp), "refresh_token": "r-new"})

        assert XaiRenewer([(f, "opencode")]).renew() is True

        assert calls == ["r-old"]
        data = json.loads(f.read_text())
        assert data["openrouter"] == {"type": "api", "key": "keep-me"}
        assert data["xai"]["refresh"] == "r-new"
        assert data["xai"]["extra"] == "kept"
        assert data["xai"]["expires"] == int(new_exp) * 1000
        assert oct(f.stat().st_mode & 0o777) == "0o600"
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".auth-")]

    def test_keeps_refresh_token_when_not_rotated(self, tmp_path, monkeypatch):
        f = _opencode(tmp_path / "a.json", time.time() - 10)
        _respond(monkeypatch, {"access_token": _jwt(time.time() + 3600)})
        XaiRenewer([(f, "opencode")]).renew()
        assert json.loads(f.read_text())["xai"]["refresh"] == "r-old"

    def test_grok_cli_file(self, tmp_path, monkeypatch):
        f = _grok(tmp_path / "auth.json", time.time() - 10)
        new = _jwt(time.time() + 3600)
        _respond(monkeypatch, {"access_token": new, "refresh_token": "g-new"})
        assert XaiRenewer([(f, "grok")]).renew() is True
        entry = json.loads(f.read_text())["https://auth.x.ai::client"]
        assert entry["key"] == new and entry["refresh_token"] == "g-new"
        assert entry["first_name"] == "A"

    def test_concurrent_cli_refresh_wins(self, tmp_path, monkeypatch):
        f = _opencode(tmp_path / "a.json", time.time() - 10)

        def cli_renews_meanwhile(refresh_token):
            _opencode(f, time.time() + 3600, refresh="r-cli")
            return {"access_token": _jwt(time.time() + 10), "refresh_token": "r-ours"}

        monkeypatch.setattr(xai_renewer, "request_refresh", cli_renews_meanwhile)
        XaiRenewer([(f, "opencode")]).renew()
        assert json.loads(f.read_text())["xai"]["refresh"] == "r-cli"

    def test_rejected_grant_writes_nothing_and_stops_retrying(self, tmp_path, monkeypatch, caplog):
        f = _opencode(tmp_path / "a.json", time.time() - 10, refresh="SECRET-REFRESH")
        before = f.read_text()
        calls = _respond(
            monkeypatch, error=xai_renewer.RefreshRejectedError("HTTP 400 invalid_grant")
        )
        renewer = XaiRenewer([(f, "opencode")])

        with caplog.at_level(logging.INFO):
            assert renewer.renew() is False
        assert f.read_text() == before
        assert "SECRET-REFRESH" not in caplog.text and "invalid_grant" in caplog.text
        assert renewer.due() is False
        assert len(calls) == 1

    def test_network_error_reports_failure(self, tmp_path, monkeypatch):
        f = _opencode(tmp_path / "a.json", time.time() - 10)
        _respond(monkeypatch, error=OSError("boom"))
        assert XaiRenewer([(f, "opencode")]).renew() is False


class TestThreadIntegration:
    def test_thread_runs_renewer_and_backs_off_per_renewer(self, tmp_path, monkeypatch):
        class Stub:
            def __init__(self, name, ok):
                self.name, self.ok, self.runs = name, ok, 0

            def due(self):
                return True

            def renew(self):
                self.runs += 1
                return self.ok

        good, bad = Stub("good", True), Stub("bad", False)
        thread = keep_alive.KeepAliveThread(
            token_path=tmp_path / "none", renewers=[good, bad], retry_tick_seconds=1000
        )
        thread.cycle_once()
        thread.cycle_once()
        assert good.runs == 2
        assert bad.runs == 1  # backed off after failing; the good one is unaffected

    def test_thread_survives_a_raising_renewer(self, tmp_path):
        class Boom:
            name = "boom"

            def due(self):
                raise RuntimeError("x")

        thread = keep_alive.KeepAliveThread(token_path=tmp_path / "none", renewers=[Boom()])
        assert thread.cycle_once() == keep_alive.TICK_SECONDS


@pytest.mark.parametrize("env", [None])
def test_grok_home_override(monkeypatch, tmp_path, env):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    assert xai_renewer._grok_path() == Path(os.environ["GROK_HOME"]) / "auth.json"
