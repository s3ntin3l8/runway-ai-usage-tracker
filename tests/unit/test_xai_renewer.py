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


class TestReviewFixes:
    def test_rate_limit_is_transient_not_a_logout(self, tmp_path, monkeypatch):
        import urllib.error

        f = _opencode(tmp_path / "a.json", time.time() - 10)

        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 429, "slow down", {}, None)

        monkeypatch.setattr(xai_renewer.urllib.request, "urlopen", boom)
        renewer = XaiRenewer([(f, "opencode")])
        assert renewer.renew() is False
        assert renewer.due() is True  # not blocked

    def test_relogin_lifts_a_rejection(self, tmp_path, monkeypatch):
        f = _opencode(tmp_path / "a.json", time.time() - 10, refresh="dead")
        _respond(monkeypatch, error=xai_renewer.RefreshRejectedError("HTTP 400 invalid_grant"))
        renewer = XaiRenewer([(f, "opencode")])
        renewer.renew()
        assert renewer.due() is False
        _opencode(f, time.time() - 10, refresh="fresh-login")
        assert renewer.due() is True

    def test_grok_uses_the_scope_the_sidecar_pushes(self, tmp_path, monkeypatch):
        f = tmp_path / "auth.json"
        f.write_text(
            json.dumps(
                {
                    "https://auth.x.ai::a": {"key": _jwt(1), "refresh_token": "ra"},
                    "https://auth.x.ai::b": {
                        "key": _jwt(1),
                        "refresh_token": "rb",
                        "email": "me@x.ai",
                    },
                }
            )
        )
        calls = _respond(monkeypatch, {"access_token": _jwt(time.time() + 3600)})
        XaiRenewer([(f, "grok")]).renew()
        assert calls == ["rb"]
        data = json.loads(f.read_text())
        assert data["https://auth.x.ai::a"]["refresh_token"] == "ra"

    def test_failed_write_warns_and_backs_off(self, tmp_path, monkeypatch, caplog):
        f = _opencode(tmp_path / "a.json", time.time() - 10)
        _respond(monkeypatch, {"access_token": _jwt(time.time() + 3600), "refresh_token": "n"})
        monkeypatch.setattr(xai_renewer.os, "replace", lambda *a: (_ for _ in ()).throw(OSError()))
        with caplog.at_level(logging.WARNING):
            assert XaiRenewer([(f, "opencode")]).renew() is False
        assert "could not write" in caplog.text
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".auth-")]

    def test_symlinked_auth_file_stays_a_symlink(self, tmp_path, monkeypatch):
        real = _opencode(tmp_path / "real.json", time.time() - 10)
        link = tmp_path / "auth.json"
        link.symlink_to(real)
        _respond(monkeypatch, {"access_token": _jwt(time.time() + 3600), "refresh_token": "n"})
        XaiRenewer([(link, "opencode")]).renew()
        assert link.is_symlink()
        assert json.loads(real.read_text())["xai"]["refresh"] == "n"


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, body: bytes = b"{}"):
    import io
    import urllib.error

    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))


class TestRequestRefresh:
    def _patch(self, monkeypatch, result):
        seen = {}

        def fake(req, timeout):
            seen["req"] = req
            if isinstance(result, Exception):
                raise result
            return _FakeResponse(result)

        monkeypatch.setattr(xai_renewer.urllib.request, "urlopen", fake)
        return seen

    def test_posts_the_refresh_grant_with_the_cli_client(self, monkeypatch):
        seen = self._patch(monkeypatch, b'{"access_token": "new", "refresh_token": "r2"}')
        assert xai_renewer.request_refresh("old-refresh")["access_token"] == "new"
        req = seen["req"]
        assert req.full_url == xai_renewer.TOKEN_ENDPOINT
        body = req.data.decode()
        assert "grant_type=refresh_token" in body
        assert "refresh_token=old-refresh" in body
        assert f"client_id={xai_renewer.CLIENT_ID}" in body
        assert req.get_method() == "POST"

    @pytest.mark.parametrize("code", [400, 401, 403])
    def test_definitive_rejections(self, monkeypatch, code):
        self._patch(monkeypatch, _http_error(code, b'{"error": "invalid_grant"}'))
        with pytest.raises(xai_renewer.RefreshRejectedError, match="invalid_grant"):
            xai_renewer.request_refresh("r")

    def test_unreadable_error_body_still_rejects(self, monkeypatch):
        self._patch(monkeypatch, _http_error(400, b"<html>"))
        with pytest.raises(xai_renewer.RefreshRejectedError, match="HTTP 400"):
            xai_renewer.request_refresh("r")

    @pytest.mark.parametrize("code", [408, 429, 500, 503])
    def test_transient_errors_are_not_rejections(self, monkeypatch, code):
        self._patch(monkeypatch, _http_error(code))
        with pytest.raises(OSError) as exc:
            xai_renewer.request_refresh("r")
        assert not isinstance(exc.value, xai_renewer.RefreshRejectedError)

    @pytest.mark.parametrize("body", [b"not json", b"[]", b'{"nope": 1}'])
    def test_bad_responses_are_errors(self, monkeypatch, body):
        self._patch(monkeypatch, body)
        with pytest.raises(OSError):
            xai_renewer.request_refresh("r")

    def test_network_failure_propagates_as_oserror(self, monkeypatch):
        self._patch(monkeypatch, OSError("down"))
        with pytest.raises(OSError):
            xai_renewer.request_refresh("r")


class TestHelpers:
    def test_jwt_expiry_normalises_milliseconds(self):
        assert xai_renewer.jwt_expiry_epoch(_jwt(1_700_000_000)) == 1_700_000_000
        ms = f"a.{base64.urlsafe_b64encode(json.dumps({'exp': 1_700_000_000_000}).encode()).decode()}.c"
        assert xai_renewer.jwt_expiry_epoch(ms) == 1_700_000_000

    @pytest.mark.parametrize("token", ["", "abc", "a.b.c", "a.e30.c"])
    def test_jwt_expiry_unreadable(self, token):
        assert xai_renewer.jwt_expiry_epoch(token) is None

    def test_expiry_falls_back_to_the_files_field(self):
        login = xai_renewer.Login(
            path=Path("x"), kind="opencode", access="opaque", refresh="r", expires_ms=5000.0
        )
        assert xai_renewer.login_expiry(login) == 5.0
        login.expires_ms = None
        assert xai_renewer.login_expiry(login) is None
        assert xai_renewer.login_due(login) is True  # unreadable counts as due

    def test_read_login_rejects_bad_files(self, tmp_path):
        missing = tmp_path / "nope.json"
        junk = tmp_path / "junk.json"
        junk.write_text("{not json")
        no_xai = tmp_path / "no_xai.json"
        no_xai.write_text(json.dumps({"openrouter": {}}))
        bad_types = tmp_path / "bad.json"
        bad_types.write_text(json.dumps({"xai": {"access": 1, "refresh": 2}}))
        for path in (missing, junk, no_xai, bad_types):
            assert xai_renewer.read_login(path, "opencode") is None
        assert xai_renewer.read_login(no_xai, "grok") is None

    def test_default_targets_cover_opencode_and_grok(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GROK_HOME", str(tmp_path))
        targets = XaiRenewer()._targets()
        assert (tmp_path / "auth.json", "grok") in targets
        assert {kind for _, kind in targets} == {"opencode", "grok"}

    def test_write_back_without_expiry_in_token_uses_expires_in(self, tmp_path):
        f = _opencode(tmp_path / "a.json", time.time() - 10)
        login = xai_renewer.read_login(f, "opencode")
        outcome = xai_renewer.write_back(
            login, {"access_token": "opaque-token", "expires_in": 3600}
        )
        assert outcome == "written"
        expires = json.loads(f.read_text())["xai"]["expires"]
        assert abs(expires / 1000 - (time.time() + 3600)) < 5

    def test_write_back_reports_failure_when_the_file_vanished(self, tmp_path):
        f = _opencode(tmp_path / "a.json", time.time() - 10)
        login = xai_renewer.read_login(f, "opencode")
        f.unlink()
        assert xai_renewer.write_back(login, {"access_token": "x"}) == "failed"

    def test_write_back_superseded_when_the_xai_entry_is_gone(self, tmp_path):
        f = _opencode(tmp_path / "a.json", time.time() - 10)
        login = xai_renewer.read_login(f, "opencode")
        f.write_text(json.dumps({"openrouter": {}}))
        assert xai_renewer.write_back(login, {"access_token": "x"}) == "superseded"

    def test_renew_with_nothing_due_is_not_a_failure(self, tmp_path, monkeypatch):
        # due() can be true and the CLI renew before renew() re-reads the file; the thread
        # must not back off five minutes for a tick that did no work.
        f = _opencode(tmp_path / "a.json", time.time() + 3600)
        calls = _respond(monkeypatch, {"access_token": "x"})
        assert XaiRenewer([(f, "opencode")]).renew() is True
        assert calls == []

    def test_superseded_write_counts_as_success(self, tmp_path, monkeypatch):
        f = _opencode(tmp_path / "a.json", time.time() - 10)
        monkeypatch.setattr(xai_renewer, "request_refresh", lambda rt: {"access_token": "x"})
        monkeypatch.setattr(xai_renewer, "write_back", lambda login, resp: "superseded")
        assert XaiRenewer([(f, "opencode")]).renew() is True


def test_concurrent_renewals_refresh_the_token_once(tmp_path, monkeypatch):
    """A keep-alive toggled off/on can leave two threads renewing; xAI rotates refresh
    tokens, so the second must find the login already renewed instead of refreshing again."""
    import threading

    f = _opencode(tmp_path / "a.json", time.time() - 10)
    calls: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def slow_refresh(refresh_token):
        calls.append(refresh_token)
        started.set()
        release.wait(5)
        return {"access_token": _jwt(time.time() + 3600), "refresh_token": "r-new"}

    monkeypatch.setattr(xai_renewer, "request_refresh", slow_refresh)
    first, second = XaiRenewer([(f, "opencode")]), XaiRenewer([(f, "opencode")])
    t1 = threading.Thread(target=first.renew)
    t2 = threading.Thread(target=second.renew)
    t1.start()
    assert started.wait(5)
    t2.start()  # blocks on the lock while t1 is mid-refresh
    release.set()
    t1.join(5)
    t2.join(5)

    assert calls == ["r-old"]
    assert json.loads(f.read_text())["xai"]["refresh"] == "r-new"
