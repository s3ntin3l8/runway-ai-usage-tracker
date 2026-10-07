"""CodexRenewer: refresh the Codex login and write it back to its ``auth.json``."""

import base64
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from app.services import token_refresher
from scripts.sidecar_pkg import codex_renewer as cr
from scripts.sidecar_pkg import keep_alive
from scripts.sidecar_pkg.codex_renewer import CodexRenewer, RefreshRejectedError

DAY = 86400


def _jwt(exp: float) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': int(exp)})}.sig"


def _auth(path: Path, expires_in: float, refresh: str = "r-old", *, age: float = 600) -> Path:
    """A Codex ``auth.json`` (mode 0600) whose access JWT expires in ``expires_in`` seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "id-old",
                    "access_token": _jwt(time.time() + expires_in),
                    "refresh_token": refresh,
                    "account_id": "acct-1",
                },
                "last_refresh": "2026-09-29T08:59:39.694099937Z",
                "custom": "kept",
            }
        )
    )
    path.chmod(0o600)
    os.utime(path, (time.time() - age, time.time() - age))
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _response(**over):
    base = {
        "access_token": _jwt(time.time() + 10 * DAY),
        "refresh_token": "r-new",
        "id_token": "id-new",
        "expires_in": 864000,
        "scope": "openid profile email offline_access",
        "token_type": "Bearer",
    }
    base.update(over)
    return base


def _respond(monkeypatch, data=None, error=None):
    calls: list[str] = []

    def fake(refresh_token):
        calls.append(refresh_token)
        if error:
            raise error
        return data if data is not None else _response()

    monkeypatch.setattr(cr, "request_refresh", fake)
    return calls


def _renewer(*paths: Path) -> CodexRenewer:
    return CodexRenewer(lambda: list(paths))


def test_constants_match_the_server_refresher():
    assert token_refresher._REFRESH_ENDPOINTS["chatgpt"] == cr.TOKEN_ENDPOINT
    assert token_refresher._PROVIDER_CLIENT_IDS["chatgpt"] == cr.CLIENT_ID


def test_the_renewer_is_named_after_its_provider():
    from app.services.refresh_policy import KEEP_ALIVE_PROVIDERS

    assert CodexRenewer.name == "chatgpt"
    assert CodexRenewer.name in KEEP_ALIVE_PROVIDERS


class TestDue:
    def test_not_due_while_the_token_has_days_left(self, tmp_path):
        f = _auth(tmp_path / "auth.json", 5 * DAY)
        assert _renewer(f).due() is False

    def test_due_inside_the_lead_window(self, tmp_path):
        f = _auth(tmp_path / "auth.json", 12 * 3600)
        assert _renewer(f).due() is True

    def test_due_once_lapsed(self, tmp_path):
        f = _auth(tmp_path / "auth.json", -3600)
        assert _renewer(f).due() is True

    def test_due_when_the_access_token_is_not_a_readable_jwt(self, tmp_path):
        f = _auth(tmp_path / "auth.json", 5 * DAY)
        data = _read(f)
        data["tokens"]["access_token"] = "opaque"
        f.write_text(json.dumps(data))
        os.utime(f, (time.time() - 600, time.time() - 600))
        assert _renewer(f).due() is True

    def test_the_short_lived_id_token_is_not_the_clock(self, tmp_path):
        f = _auth(tmp_path / "auth.json", 5 * DAY)
        data = _read(f)
        data["tokens"]["id_token"] = _jwt(time.time() - 3600)  # expired an hour ago, as usual
        f.write_text(json.dumps(data))
        os.utime(f, (time.time() - 600, time.time() - 600))
        assert _renewer(f).due() is False

    def test_nothing_to_renew_without_a_file_tokens_or_a_refresh_token(self, tmp_path):
        no_tokens = tmp_path / "a.json"
        # API-key mode login: no ``tokens`` block at all.
        no_tokens.write_text(json.dumps({"auth_mode": "apikey"}))
        blank = _auth(tmp_path / "b.json", -10, refresh="")
        junk = tmp_path / "c.json"
        junk.write_text("{not json")
        r = _renewer(tmp_path / "missing.json", no_tokens, blank, junk)
        assert r.due() is False
        assert r.renew() is True  # nothing due is not a failure

    def test_a_file_written_a_moment_ago_is_left_alone(self, tmp_path):
        f = _auth(tmp_path / "auth.json", -10, age=5)
        assert _renewer(f).due() is False
        os.utime(f, (time.time() - 60, time.time() - 60))
        assert _renewer(f).due() is True

    def test_login_due_boundaries(self):
        login = cr.Login(Path("x"), "r", expires_at=1_000_000.0)
        assert cr.login_due(login, now=1_000_000 - cr.LEAD_SECONDS - 1) is False
        assert cr.login_due(login, now=1_000_000 - cr.LEAD_SECONDS) is True
        assert cr.login_due(login, now=2_000_000) is True


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
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))


class TestRequestRefresh:
    def _patch(self, monkeypatch, result):
        seen = {}

        def fake(req, timeout):
            seen["req"] = req
            if isinstance(result, Exception):
                raise result
            return _FakeResponse(result)

        monkeypatch.setattr(cr.urllib.request, "urlopen", fake)
        return seen

    def test_sends_exactly_codexs_request(self, monkeypatch):
        """Form body of grant_type/refresh_token/client_id — no scope — as Codex 0.161 sends it."""
        seen = self._patch(monkeypatch, b'{"access_token": "n", "refresh_token": "r2"}')
        out = cr.request_refresh("old-refresh")
        assert out["access_token"] == "n"
        req = seen["req"]
        assert req.full_url == "https://auth.openai.com/oauth/token"
        assert req.get_method() == "POST"
        assert dict(urllib.parse.parse_qsl(req.data.decode())) == {
            "grant_type": "refresh_token",
            "refresh_token": "old-refresh",
            "client_id": cr.CLIENT_ID,
        }
        headers = {k.lower(): v for k, v in req.header_items()}
        assert headers["content-type"] == "application/x-www-form-urlencoded"
        assert headers["user-agent"] == cr.USER_AGENT

    @pytest.mark.parametrize("code", [400, 401, 403])
    def test_a_dead_expired_or_revoked_token_is_a_rejection(self, monkeypatch, code):
        self._patch(
            monkeypatch,
            _http_error(code, b'{"error": {"code": "refresh_token_expired", "message": "x"}}'),
        )
        with pytest.raises(RefreshRejectedError, match="refresh_token_expired"):
            cr.request_refresh("r")

    def test_a_string_error_code_is_read_too(self, monkeypatch):
        self._patch(monkeypatch, _http_error(400, b'{"error": "invalid_grant"}'))
        with pytest.raises(RefreshRejectedError, match="invalid_grant"):
            cr.request_refresh("r")

    def test_an_unreadable_error_body_still_rejects(self, monkeypatch):
        self._patch(monkeypatch, _http_error(401, b"<html>"))
        with pytest.raises(RefreshRejectedError, match="HTTP 401"):
            cr.request_refresh("r")

    @pytest.mark.parametrize("code", [408, 429, 500, 503])
    def test_throttling_and_server_errors_are_transient(self, monkeypatch, code):
        self._patch(monkeypatch, _http_error(code))
        with pytest.raises(OSError) as exc:
            cr.request_refresh("r")
        assert not isinstance(exc.value, RefreshRejectedError)

    @pytest.mark.parametrize("body", [b"not json", b"[]", b'{"nope": 1}'])
    def test_bad_success_bodies_are_errors(self, monkeypatch, body):
        self._patch(monkeypatch, body)
        with pytest.raises(OSError):
            cr.request_refresh("r")

    def test_network_failure_is_an_oserror(self, monkeypatch):
        self._patch(monkeypatch, OSError("down"))
        with pytest.raises(OSError):
            cr.request_refresh("r")


class TestWriteBack:
    def test_updates_only_the_tokens_and_last_refresh(self, tmp_path):
        f = _auth(tmp_path / "auth.json", -10)
        before = _read(f)
        assert cr.write_back(cr.read_login(f), _response()) == "written"

        after = _read(f)
        tokens = after["tokens"]
        assert tokens["refresh_token"] == "r-new" and tokens["id_token"] == "id-new"
        assert tokens["access_token"] != before["tokens"]["access_token"]
        assert tokens["account_id"] == "acct-1"
        assert after["auth_mode"] == "chatgpt" and after["OPENAI_API_KEY"] is None
        assert after["custom"] == "kept"
        assert set(after) == set(before) and set(tokens) == set(before["tokens"])
        assert after["last_refresh"] != before["last_refresh"]
        assert after["last_refresh"].endswith("Z")

    def test_keeps_the_old_id_token_when_the_response_has_none(self, tmp_path):
        f = _auth(tmp_path / "auth.json", -10)
        resp = _response()
        del resp["id_token"]
        cr.write_back(cr.read_login(f), resp)
        assert _read(f)["tokens"]["id_token"] == "id-old"

    def test_always_keeps_the_new_refresh_token_because_the_old_one_is_rotated(self, tmp_path):
        f = _auth(tmp_path / "auth.json", -10)
        resp = _response()
        del resp["refresh_token"]  # never seen, but never lose a token over it
        assert cr.write_back(cr.read_login(f), resp) == "written"
        assert _read(f)["tokens"]["refresh_token"] == "r-old"

    def test_keeps_the_file_mode_and_leaves_no_temp_file(self, tmp_path):
        f = _auth(tmp_path / "auth.json", -10)
        cr.write_back(cr.read_login(f), _response())
        assert oct(f.stat().st_mode & 0o777) == "0o600"
        assert [p.name for p in tmp_path.iterdir()] == ["auth.json"]

    def test_codex_renewing_first_wins(self, tmp_path):
        f = _auth(tmp_path / "auth.json", -10)
        login = cr.read_login(f)
        _auth(f, 10 * DAY, refresh="r-cli")  # Codex refreshed meanwhile
        assert cr.write_back(login, _response()) == "superseded"
        assert _read(f)["tokens"]["refresh_token"] == "r-cli"

    def test_a_vanished_file_or_entry_is_not_a_crash(self, tmp_path):
        f = _auth(tmp_path / "auth.json", -10)
        login = cr.read_login(f)
        f.write_text(json.dumps({"auth_mode": "apikey"}))
        assert cr.write_back(login, _response()) == "superseded"
        f.unlink()
        assert cr.write_back(login, _response()) == "failed"

    def test_a_failed_write_cleans_up_after_itself(self, tmp_path, monkeypatch):
        f = _auth(tmp_path / "auth.json", -10)
        login = cr.read_login(f)

        def boom(*_a):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        assert cr.write_back(login, _response()) == "failed"
        assert [p.name for p in tmp_path.iterdir()] == ["auth.json"]
        assert _read(f)["tokens"]["refresh_token"] == "r-old"

    def test_a_symlinked_auth_file_stays_a_symlink(self, tmp_path):
        real = _auth(tmp_path / "dotfiles" / "auth.json", -10)
        link = tmp_path / ".codex" / "auth.json"
        link.parent.mkdir()
        link.symlink_to(real)
        os.utime(real, (time.time() - 600, time.time() - 600))
        assert cr.write_back(cr.read_login(link), _response()) == "written"
        assert link.is_symlink()
        assert _read(real)["tokens"]["refresh_token"] == "r-new"


class TestRenew:
    def test_renews_a_due_login_and_writes_it_back(self, tmp_path, monkeypatch):
        f = _auth(tmp_path / "auth.json", -10)
        calls = _respond(monkeypatch)
        assert _renewer(f).renew() is True
        assert calls == ["r-old"]
        assert _read(f)["tokens"]["refresh_token"] == "r-new"

    def test_each_login_is_renewed_independently(self, tmp_path, monkeypatch):
        a = _auth(tmp_path / "a" / "auth.json", -10, refresh="ra")
        b = _auth(tmp_path / "b" / "auth.json", -10, refresh="rb")
        fresh = _auth(tmp_path / "c" / "auth.json", 6 * DAY, refresh="rc")
        calls = _respond(monkeypatch)
        assert _renewer(a, b, fresh).renew() is True
        assert sorted(calls) == ["ra", "rb"]
        assert _read(fresh)["tokens"]["refresh_token"] == "rc"

    def test_a_file_reachable_through_two_paths_is_renewed_once(self, tmp_path, monkeypatch):
        real = _auth(tmp_path / "real" / "auth.json", -10)
        link = tmp_path / "link.json"
        link.symlink_to(real)
        calls = _respond(monkeypatch)
        _renewer(real, link).renew()
        assert len(calls) == 1

    def test_a_dead_login_stays_blocked_whichever_path_reaches_it(self, tmp_path, monkeypatch):
        """The rejection memo is keyed on the resolved file, so ``~/.codex`` and a ``CODEX_HOME``
        pointing at the same ``auth.json`` are one login, in either order."""
        real = _auth(tmp_path / "real" / "auth.json", -10)
        link = tmp_path / "link.json"
        link.symlink_to(real)
        current = [real]
        r = CodexRenewer(lambda: list(current))
        calls = _respond(monkeypatch, error=RefreshRejectedError("HTTP 400 refresh_token_expired"))
        r.renew()
        current[:] = [link]  # the same file, reached the other way
        assert r.due() is False
        r.renew()
        assert len(calls) == 1

    def test_targets_are_re_evaluated_on_every_tick(self, tmp_path, monkeypatch):
        first = _auth(tmp_path / "one" / "auth.json", 6 * DAY)
        second = _auth(tmp_path / "two" / "auth.json", -10)
        current = [first]
        r = CodexRenewer(lambda: list(current))
        assert r.due() is False
        current.append(second)  # a config reload added a CODEX_HOME
        assert r.due() is True
        calls = _respond(monkeypatch)
        r.renew()
        assert len(calls) == 1

    def test_a_rejection_after_codex_renewed_first_is_not_a_logout(
        self, tmp_path, monkeypatch, caplog
    ):
        f = _auth(tmp_path / "auth.json", -10)

        def cli_renews_first(refresh_token):
            _auth(f, 10 * DAY, refresh="r-cli", age=2)
            raise RefreshRejectedError("HTTP 400 refresh_token_reused")

        monkeypatch.setattr(cr, "request_refresh", cli_renews_first)
        r = _renewer(f)
        with caplog.at_level(logging.INFO):
            assert r.renew() is True
        assert "renewed by Codex first" in caplog.text
        assert r._rejected == {}
        assert _read(f)["tokens"]["refresh_token"] == "r-cli"

    def test_a_rejection_with_an_unchanged_file_is_a_dead_login(
        self, tmp_path, monkeypatch, caplog
    ):
        f = _auth(tmp_path / "auth.json", -10, refresh="SECRET-REFRESH")
        before = f.read_text()
        calls = _respond(monkeypatch, error=RefreshRejectedError("HTTP 401 refresh_token_expired"))
        r = _renewer(f)
        with caplog.at_level(logging.INFO):
            assert r.renew() is False
        assert f.read_text() == before
        assert "codex login" in caplog.text and "refresh_token_expired" in caplog.text
        assert "SECRET-REFRESH" not in caplog.text
        assert r.due() is False  # not retried with the same dead token…
        r.renew()
        assert len(calls) == 1
        _auth(f, -10, refresh="fresh-login")  # …until the user logs in again
        assert r.due() is True

    @pytest.mark.parametrize("code", [429, 500])
    def test_throttling_is_transient_so_nothing_is_blocked_or_written(
        self, tmp_path, monkeypatch, code
    ):
        f = _auth(tmp_path / "auth.json", -10)
        before = f.read_text()
        _respond(monkeypatch, error=OSError(f"HTTP {code}"))
        r = _renewer(f)
        assert r.renew() is False
        assert f.read_text() == before
        assert r.due() is True and r._rejected == {}

    def test_a_failed_save_is_reported_as_a_failure(self, tmp_path, monkeypatch, caplog):
        f = _auth(tmp_path / "auth.json", -10)
        _respond(monkeypatch)
        monkeypatch.setattr(cr, "atomic_replace_json", lambda *a, **k: False)
        with caplog.at_level(logging.WARNING):
            assert _renewer(f).renew() is False
        assert "could not write the renewed login" in caplog.text

    def test_a_superseded_write_counts_as_success(self, tmp_path, monkeypatch):
        f = _auth(tmp_path / "auth.json", -10)
        _respond(monkeypatch)
        monkeypatch.setattr(cr, "write_back", lambda login, resp: "superseded")
        assert _renewer(f).renew() is True

    def test_never_logs_token_material(self, tmp_path, monkeypatch, caplog):
        f = _auth(tmp_path / "auth.json", -10, refresh="R-SECRET-1")
        _respond(monkeypatch, data=_response(access_token="A-SECRET-2", refresh_token="R-SECRET-3"))
        with caplog.at_level(logging.DEBUG):
            _renewer(f).renew()
        for secret in ("R-SECRET-1", "A-SECRET-2", "R-SECRET-3", "id-new", "id-old"):
            assert secret not in caplog.text

    def test_concurrent_renewals_refresh_the_token_once(self, tmp_path, monkeypatch):
        f = _auth(tmp_path / "auth.json", -10)
        calls: list[str] = []
        started, release = threading.Event(), threading.Event()

        def slow(refresh_token):
            calls.append(refresh_token)
            started.set()
            release.wait(5)
            return _response()

        monkeypatch.setattr(cr, "request_refresh", slow)
        first, second = _renewer(f), _renewer(f)
        t1 = threading.Thread(target=first.renew)
        t2 = threading.Thread(target=second.renew)
        t1.start()
        assert started.wait(5)
        t2.start()  # blocks on the lock while t1 is mid-refresh
        release.set()
        t1.join(5)
        t2.join(5)
        assert calls == ["r-old"]
        assert _read(f)["tokens"]["refresh_token"] == "r-new"


class TestKeepAliveThread:
    def test_a_failing_renewal_backs_off_that_renewer_only(self, tmp_path, monkeypatch):
        f = _auth(tmp_path / "auth.json", -10)
        monkeypatch.setattr(
            cr, "request_refresh", lambda *a, **k: (_ for _ in ()).throw(OSError("429"))
        )

        class Other:
            name = "other"
            runs = 0

            def due(self):
                return True

            def renew(self):
                Other.runs += 1
                return True

        thread = keep_alive.KeepAliveThread(
            token_path=tmp_path / "none", renewers=[_renewer(f), Other()], retry_tick_seconds=1000
        )
        thread.cycle_once()
        thread.cycle_once()
        assert "chatgpt" in thread._renewer_resume_at
        assert Other.runs == 2


class TestLostTokenSafety:
    """The old refresh token is rotated the moment the endpoint answers: never lose the new one."""

    def test_a_failed_save_is_retried_without_another_exchange(self, tmp_path, monkeypatch):
        f = _auth(tmp_path / "auth.json", -10)
        calls = _respond(monkeypatch)
        real = cr.atomic_replace_json
        monkeypatch.setattr(cr, "atomic_replace_json", lambda *a, **k: False)
        r = _renewer(f)
        assert r.renew() is False  # exchanged, but could not save
        assert len(calls) == 1 and _read(f)["tokens"]["refresh_token"] == "r-old"

        monkeypatch.setattr(cr, "atomic_replace_json", real)  # the disk recovered
        assert r.renew() is True
        assert len(calls) == 1
        assert _read(f)["tokens"]["refresh_token"] == "r-new"
        assert r._unsaved == {}

    def test_a_stored_response_is_never_saved_over_a_newer_login(self, tmp_path, monkeypatch):
        f = _auth(tmp_path / "auth.json", -10)
        _respond(monkeypatch)
        monkeypatch.setattr(cr, "atomic_replace_json", lambda *a, **k: False)
        r = _renewer(f)
        r.renew()
        assert len(r._unsaved) == 1
        _auth(f, 10 * DAY, refresh="r-cli")  # Codex refreshed itself meanwhile
        assert r.due() is False
        assert _read(f)["tokens"]["refresh_token"] == "r-cli"
        monkeypatch.undo()
        calls = _respond(monkeypatch)
        _auth(f, -10, refresh="r-cli")
        assert r.renew() is True
        assert calls == ["r-cli"]
        assert r._unsaved == {}
