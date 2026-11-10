"""MiniMaxCodeRenewer: refresh the MiniMax Code login and write it back into its own file."""

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

import pytest

from scripts.sidecar_pkg import minimax_code_renewer as mcr
from scripts.sidecar_pkg.minimax_code_renewer import MiniMaxCodeRenewer, RefreshRejectedError

HOUR = 3600
# The access token lives twelve hours.
TOKEN_LIFETIME = 12 * HOUR
SERVICE = "com.minimax.mcode.oauth.prod.en"


def _client_dir(tmp_path: Path, *, region: str = "en", build_env: str = "prod") -> Path:
    """The directory the CLI keeps a login in: ``auth/<buildEnv>/<region>/mcode-public/``."""
    directory = tmp_path / "auth" / build_env / region / "mcode-public"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_login(
    directory: Path,
    *,
    expires_in: float = TOKEN_LIFETIME,
    refresh: str = "r-old",
    generation: int = 7,
    age: float = 600,
    service: str = SERVICE,
) -> Path:
    """A MiniMax Code ``auth.json`` (mode 0600) shaped exactly like the CLI writes it."""
    record = {
        "schemaVersion": 1,
        "accessToken": "a-old",
        "refreshToken": refresh,
        "tokenType": "Bearer",
        "clientId": "mcode-public",
        "scopes": ["agent.default"],
        "audience": "agent-backend",
        "expiresAtMs": int((time.time() + expires_in) * 1000),
        "generation": generation,
        "loginEpoch": "epoch-1",
    }
    path = directory / "auth.json"
    path.write_text(json.dumps({"schemaVersion": 1, "records": {f"{service}\u0000acct-1": record}}))
    path.chmod(0o600)
    os.utime(path, (time.time() - age, time.time() - age))
    return path


def _write_state(
    directory: Path,
    *,
    status: str = "authenticated",
    store_kind: str = "file",
    generation: int = 7,
    extra: dict | None = None,
) -> Path:
    """A MiniMax Code ``auth-state.json`` (mode 0600)."""
    state = {
        "schemaVersion": 2,
        "status": status,
        "storeKind": store_kind,
        "clientId": "mcode-public",
        "scopes": ["agent.default"],
        "audience": "agent-backend",
        "buildEnv": "prod",
        "region": "en",
        "generation": generation,
        "expiresAtMs": int((time.time() + TOKEN_LIFETIME) * 1000),
    }
    if extra:
        state.update(extra)
    path = directory / "auth-state.json"
    path.write_text(json.dumps(state))
    path.chmod(0o600)
    return path


def _login(tmp_path: Path, **kwargs) -> Path:
    """A login directory with both files; returns the ``auth.json`` path."""
    directory = _client_dir(tmp_path, **kwargs)
    path = _write_login(directory)
    _write_state(directory)
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _record(path: Path) -> dict:
    return next(iter(_read(path)["records"].values()))


def _response(**over) -> dict:
    body = {
        "access_token": "a-new",
        "refresh_token": "r-new",
        "token_type": "Bearer",
        "expires_in": TOKEN_LIFETIME,
        "scope": "agent.default",
    }
    body.update(over)
    return body


def _renewer(*paths: Path) -> MiniMaxCodeRenewer:
    return MiniMaxCodeRenewer(lambda: list(paths))


class TestDiscovery:
    def test_the_renewer_is_named_after_its_provider(self):
        from app.services.refresh_policy import KEEP_ALIVE_PROVIDERS

        assert MiniMaxCodeRenewer.name == "minimax"
        assert MiniMaxCodeRenewer.name in KEEP_ALIVE_PROVIDERS

    def test_it_reads_the_login_the_cli_wrote(self, tmp_path):
        path = _login(tmp_path)
        logins = mcr.read_logins(path)
        assert len(logins) == 1
        login = logins[0]
        assert login.refresh == "r-old"
        assert login.generation == 7
        assert login.region == "en"
        assert login.expires_at is not None
        assert login.state_path == path.parent / "auth-state.json"

    @pytest.mark.parametrize(
        ("region", "endpoint"),
        [
            ("en", "https://account.minimax.io/oauth2/token"),
            ("cn", "https://account.minimax.cn/oauth2/token"),
        ],
    )
    def test_the_region_picks_the_account_service(self, region, endpoint):
        assert mcr.token_endpoint(region) == endpoint

    def test_the_endpoint_can_be_overridden(self, monkeypatch):
        monkeypatch.setenv("MCODE_OAUTH_TOKEN_ENDPOINT", "http://127.0.0.1:9/oauth2/token")
        assert mcr.token_endpoint("en") == "http://127.0.0.1:9/oauth2/token"

    def test_a_record_without_a_refresh_token_is_not_a_login(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, refresh="")
        assert mcr.read_logins(path) == []

    def test_a_file_that_is_not_a_mcode_login_is_ignored(self, tmp_path):
        path = tmp_path / "auth.json"
        path.write_text(json.dumps({"schemaVersion": 1, "records": {"other\u0000a": {}}}))
        assert mcr.read_logins(path) == []


class TestDue:
    def test_not_due_while_the_token_has_hours_left(self, tmp_path):
        path = _login(tmp_path)
        assert _renewer(path).due() is False

    def test_due_inside_the_lead_window(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=mcr.LEAD_SECONDS - 60)
        _write_state(directory)
        assert _renewer(path).due() is True

    def test_not_due_just_outside_the_lead_window(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=mcr.LEAD_SECONDS + 60)
        _write_state(directory)
        assert _renewer(path).due() is False

    def test_due_once_lapsed(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        assert _renewer(path).due() is True

    def test_due_when_the_expiry_is_unreadable(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory)
        _write_state(directory)
        data = _read(path)
        del next(iter(data["records"].values()))["expiresAtMs"]
        path.write_text(json.dumps(data))
        os.utime(path, (time.time() - 600, time.time() - 600))
        assert _renewer(path).due() is True

    def test_not_due_when_the_login_lives_in_the_os_keyring(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory, store_kind="os-keyring")
        assert _renewer(path).due() is False

    @pytest.mark.parametrize("status", ["refreshing", "authorizing", "error", "anonymous"])
    def test_not_due_while_the_app_is_otherwise_busy(self, tmp_path, status):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory, status=status)
        assert _renewer(path).due() is False

    def test_not_due_while_the_app_has_a_refresh_in_flight(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory, extra={"refresh": {"id": "r-1", "owner": "app"}})
        assert _renewer(path).due() is False

    def test_not_due_while_the_app_has_a_credential_staged(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        pending = directory / "refresh"
        pending.mkdir()
        (pending / "auth.json").write_text(json.dumps({"schemaVersion": 1, "records": {}}))
        assert _renewer(path).due() is False

    def test_not_due_without_the_state_file(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        assert _renewer(path).due() is False

    def test_not_due_when_the_state_names_another_generation(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60, generation=7)
        _write_state(directory, generation=8)
        assert _renewer(path).due() is False

    def test_a_file_written_a_moment_ago_is_left_alone(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60, age=5)
        _write_state(directory)
        assert _renewer(path).due() is False
        os.utime(path, (time.time() - 60, time.time() - 60))
        assert _renewer(path).due() is True

    def test_nothing_due_is_not_a_failure(self, tmp_path):
        path = _login(tmp_path)
        assert _renewer(path).renew() is True


class TestRequestRefresh:
    def test_sends_exactly_the_clients_request(self, monkeypatch, tmp_path):
        """The MiniMax Code CLI's own form body: grant_type, refresh_token, client_id, scope, audience."""
        seen = {}

        class _Resp:
            def read(self):
                return json.dumps(_response()).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake(req, timeout):
            seen["req"] = req
            return _Resp()

        monkeypatch.setattr(mcr.urllib.request, "urlopen", fake)
        path = _login(tmp_path)
        out = mcr.request_refresh(mcr.read_logins(path)[0])
        assert out["access_token"] == "a-new"

        req = seen["req"]
        assert req.full_url == "https://account.minimax.io/oauth2/token"
        assert req.get_method() == "POST"
        assert dict(parse_qsl(req.data.decode())) == {
            "grant_type": "refresh_token",
            "refresh_token": "r-old",
            "client_id": "mcode-public",
            "scope": "agent.default",
            "audience": "agent-backend",
        }
        headers = {k.lower(): v for k, v in req.header_items()}
        assert headers["content-type"] == "application/x-www-form-urlencoded"
        assert headers["user-agent"] == mcr.USER_AGENT

    def test_the_cn_login_uses_the_cn_account_service(self, monkeypatch, tmp_path):
        seen = {}

        class _Resp:
            def read(self):
                return json.dumps(_response()).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(
            mcr.urllib.request, "urlopen", lambda req, timeout: (seen.update(req=req), _Resp())[1]
        )
        directory = _client_dir(tmp_path, region="cn")
        path = _write_login(directory)
        mcr.request_refresh(mcr.read_logins(path)[0])
        assert seen["req"].full_url == "https://account.minimax.cn/oauth2/token"

    @pytest.mark.parametrize("code", [400, 401, 403])
    def test_a_dead_expired_or_revoked_token_is_a_rejection(self, monkeypatch, tmp_path, code):
        import urllib.error

        def fake(req, timeout):
            raise urllib.error.HTTPError(
                "u", code, "x", {}, __import__("io").BytesIO(b'{"error": "invalid_grant"}')
            )

        monkeypatch.setattr(mcr.urllib.request, "urlopen", fake)
        path = _login(tmp_path)
        with pytest.raises(RefreshRejectedError, match="invalid_grant"):
            mcr.request_refresh(mcr.read_logins(path)[0])

    @pytest.mark.parametrize("code", [408, 429, 500, 503])
    def test_throttling_and_server_errors_are_transient(self, monkeypatch, tmp_path, code):
        import urllib.error

        def fake(req, timeout):
            raise urllib.error.HTTPError("u", code, "x", {}, __import__("io").BytesIO(b"{}"))

        monkeypatch.setattr(mcr.urllib.request, "urlopen", fake)
        path = _login(tmp_path)
        with pytest.raises(OSError) as exc:
            mcr.request_refresh(mcr.read_logins(path)[0])
        assert not isinstance(exc.value, RefreshRejectedError)

    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            b'{"nope": 1}',
            b'{"access_token": "a", "token_type": "Bearer"}',
            b'{"access_token": "a", "token_type": "Basic", "expires_in": 10}',
            b'{"access_token": "a", "token_type": "Bearer", "expires_in": 10, "scope": "other"}',
        ],
    )
    def test_bad_success_bodies_are_errors(self, monkeypatch, tmp_path, body):
        class _Resp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(mcr.urllib.request, "urlopen", lambda req, timeout: _Resp())
        path = _login(tmp_path)
        with pytest.raises(OSError):
            mcr.request_refresh(mcr.read_logins(path)[0])


class TestWriteBack:
    def test_updates_only_the_tokens_and_the_expiry(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        state_path = _write_state(directory)
        before = _read(path)
        assert mcr.write_back(mcr.read_logins(path)[0], _response()) == "written"

        after = _read(path)
        assert set(after) == set(before)
        assert set(after["records"]) == set(before["records"])
        record = _record(path)
        assert record["accessToken"] == "a-new"
        assert record["refreshToken"] == "r-new"
        assert record["expiresAtMs"] > int(time.time() * 1000)
        # The identity of the login is untouched.
        assert record["generation"] == 7
        assert record["loginEpoch"] == "epoch-1"
        assert record["clientId"] == "mcode-public"
        assert record["scopes"] == ["agent.default"]
        assert record["audience"] == "agent-backend"
        assert record["schemaVersion"] == 1
        assert record["tokenType"] == "Bearer"

        state = _read(state_path)
        assert state["expiresAtMs"] == record["expiresAtMs"]
        assert state["generation"] == 7
        assert state["status"] == "authenticated"

    def test_an_unknown_key_survives(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        data = _read(path)
        next(iter(data["records"].values()))["custom"] = "kept"
        path.write_text(json.dumps(data))
        os.utime(path, (time.time() - 600, time.time() - 600))
        mcr.write_back(mcr.read_logins(path)[0], _response())
        assert _record(path)["custom"] == "kept"

    def test_it_keeps_the_old_refresh_token_when_the_response_has_none(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        response = _response()
        del response["refresh_token"]
        assert mcr.write_back(mcr.read_logins(path)[0], response) == "written"
        assert _record(path)["refreshToken"] == "r-old"

    def test_the_generation_is_never_bumped(self, tmp_path):
        """The CLI answers ``status: error`` when the two generations disagree."""
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60, generation=7)
        state_path = _write_state(directory, generation=7)
        mcr.write_back(mcr.read_logins(path)[0], _response())
        assert _record(path)["generation"] == 7
        assert _read(state_path)["generation"] == 7

    def test_it_keeps_the_file_mode_and_leaves_no_temp_file(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        mcr.write_back(mcr.read_logins(path)[0], _response())
        if os.name != "nt":
            assert oct(path.stat().st_mode & 0o777) == "0o600"
            assert oct((directory / "auth-state.json").stat().st_mode & 0o777) == "0o600"
        assert sorted(p.name for p in directory.iterdir()) == ["auth-state.json", "auth.json"]

    def test_minimax_renewing_first_wins(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        login = mcr.read_logins(path)[0]
        _write_login(directory, expires_in=TOKEN_LIFETIME, refresh="r-app")  # the app refreshed
        assert mcr.write_back(login, _response()) == "superseded"
        assert _record(path)["refreshToken"] == "r-app"

    def test_a_newer_generation_supersedes_our_result(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60, generation=7)
        _write_state(directory, generation=7)
        login = mcr.read_logins(path)[0]
        _write_login(directory, expires_in=-60, generation=8, refresh="r-app")
        _write_state(directory, generation=8)
        assert mcr.write_back(login, _response()) == "superseded"

    def test_the_app_mid_refresh_supersedes_our_result(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        login = mcr.read_logins(path)[0]
        _write_state(directory, status="refreshing")
        assert mcr.write_back(login, _response()) == "superseded"

    def test_a_vanished_file_or_entry_is_not_a_crash(self, tmp_path):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        login = mcr.read_logins(path)[0]
        path.write_text(json.dumps({"schemaVersion": 1, "records": {}}))
        assert mcr.write_back(login, _response()) == "superseded"
        path.unlink()
        assert mcr.write_back(login, _response()) == "failed"

    def test_a_failed_write_cleans_up_after_itself(self, tmp_path, monkeypatch):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        login = mcr.read_logins(path)[0]

        def boom(*_a):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        assert mcr.write_back(login, _response()) == "failed"
        assert sorted(p.name for p in directory.iterdir()) == ["auth-state.json", "auth.json"]
        assert _record(path)["refreshToken"] == "r-old"


class TestRenew:
    def test_renews_a_due_login_and_writes_it_back(self, tmp_path, monkeypatch):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        calls = []

        def fake(req, timeout):
            calls.append(req.data.decode())
            return type(
                "R",
                (),
                {
                    "read": lambda self: json.dumps(_response()).encode(),
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, *e: False,
                },
            )()

        monkeypatch.setattr(mcr.urllib.request, "urlopen", fake)
        assert _renewer(path).renew() is True
        assert len(calls) == 1
        assert _record(path)["accessToken"] == "a-new"
        assert _record(path)["generation"] == 7

    def test_a_rejected_login_is_not_retried_with_the_same_token(self, tmp_path, monkeypatch):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        calls = []

        def fake(req, timeout):
            calls.append(req)
            raise mcr.RefreshRejectedError("HTTP 400 invalid_grant")

        monkeypatch.setattr(mcr.urllib.request, "urlopen", fake)
        renewer = _renewer(path)
        assert renewer.renew() is False  # the rejection is a failure
        assert renewer.renew() is True  # the login is blocked now: nothing is due
        assert len(calls) == 1

    def test_a_new_login_lifts_the_block(self, tmp_path, monkeypatch):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        calls = []

        def fake(req, timeout):
            calls.append(req)
            raise mcr.RefreshRejectedError("HTTP 400 invalid_grant")

        monkeypatch.setattr(mcr.urllib.request, "urlopen", fake)
        renewer = _renewer(path)
        assert renewer.renew() is False
        _write_login(directory, expires_in=-60, refresh="r-new-login")  # the user logged in again
        _write_state(directory)
        assert renewer.renew() is False  # still failing, but it was tried again
        assert len(calls) == 2

    def test_a_transient_failure_is_retried_on_the_next_tick(self, tmp_path, monkeypatch):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        calls = []

        def fake(req, timeout):
            calls.append(req)
            if len(calls) == 1:
                raise OSError("HTTP 500")
            return type(
                "R",
                (),
                {
                    "read": lambda self: json.dumps(_response()).encode(),
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, *e: False,
                },
            )()

        monkeypatch.setattr(mcr.urllib.request, "urlopen", fake)
        renewer = _renewer(path)
        assert renewer.renew() is False
        assert _record(path)["refreshToken"] == "r-old"
        assert renewer.renew() is True
        assert _record(path)["refreshToken"] == "r-new"

    def test_a_failed_save_is_retried_without_spending_another_token(self, tmp_path, monkeypatch):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)
        calls = []

        def fake(req, timeout):
            calls.append(req)
            return type(
                "R",
                (),
                {
                    "read": lambda self: json.dumps(_response()).encode(),
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, *e: False,
                },
            )()

        monkeypatch.setattr(mcr.urllib.request, "urlopen", fake)
        renewer = _renewer(path)
        real_write = mcr.write_back
        outcomes = iter(["failed", "written"])

        def flaky(login, response):
            outcome = next(outcomes)
            if outcome == "failed":
                return outcome
            return real_write(login, response)

        monkeypatch.setattr(mcr, "write_back", flaky)
        assert renewer.renew() is False
        assert renewer.renew() is True
        assert len(calls) == 1  # the token was spent once; only the save was retried
        assert _record(path)["refreshToken"] == "r-new"

    def test_a_failed_state_write_is_repaired_on_the_next_tick(self, tmp_path, monkeypatch):
        """The credential landed but the state file did not: the next tick repairs the state."""
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        state_path = _write_state(directory)
        calls = []

        def fake(req, timeout):
            calls.append(req)
            return type(
                "R",
                (),
                {
                    "read": lambda self: json.dumps(_response()).encode(),
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, *e: False,
                },
            )()

        monkeypatch.setattr(mcr.urllib.request, "urlopen", fake)
        real_write = mcr.write_back
        state_writes = []

        def flaky(login, response):
            # Fail only the state write: the credential write lands first.
            if not state_writes:
                state_writes.append(True)
                return real_write(login, response)
            return "failed"

        def fail_state(login, response):
            outcome = flaky(login, response)
            if outcome == "written":
                # Undo the state write to simulate the file write failing after the credential.
                state = _read(state_path)
                state["expiresAtMs"] = 0
                state_path.write_text(json.dumps(state))
                return "failed"
            return outcome

        monkeypatch.setattr(mcr, "write_back", fail_state)
        renewer = _renewer(path)
        assert renewer.renew() is False
        assert _record(path)["accessToken"] == "a-new"  # the credential landed
        assert _read(state_path)["expiresAtMs"] == 0  # the state did not

        monkeypatch.setattr(mcr, "write_back", real_write)
        assert renewer.renew() is True
        assert len(calls) == 1  # the token was spent once
        assert _read(state_path)["expiresAtMs"] == _record(path)["expiresAtMs"]
        assert _read(state_path)["generation"] == 7

    def test_no_token_material_reaches_the_log(self, tmp_path, monkeypatch, caplog):
        directory = _client_dir(tmp_path)
        path = _write_login(directory, expires_in=-60)
        _write_state(directory)

        def fake(req, timeout):
            raise mcr.RefreshRejectedError("HTTP 400 invalid_grant")

        monkeypatch.setattr(mcr.urllib.request, "urlopen", fake)
        with caplog.at_level(logging.DEBUG, logger=mcr.logger.name):
            _renewer(path).renew()
        text = caplog.text
        assert "r-old" not in text
        assert "a-old" not in text


class _StubOAuthHandler(BaseHTTPRequestHandler):
    """Answers ``POST /oauth2/token`` with a canned token response and records the request."""

    received: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's name
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        type(self).received.append(
            {"path": self.path, "form": dict(parse_qsl(body)), "headers": dict(self.headers)}
        )
        payload = json.dumps(_response()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep the test output quiet
        pass


@pytest.fixture
def stub_oauth_server():
    """A local OAuth server standing in for ``account.minimax.io``."""
    _StubOAuthHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubOAuthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/oauth2/token", _StubOAuthHandler.received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_renewal_works_against_a_real_http_server(tmp_path, monkeypatch, stub_oauth_server):
    """The whole path -- request, response, write-back -- over real HTTP, against a local stub.

    No real token is spent: the stub server answers for ``account.minimax.io``.
    """
    endpoint, received = stub_oauth_server
    monkeypatch.setenv("MCODE_OAUTH_TOKEN_ENDPOINT", endpoint)
    directory = _client_dir(tmp_path)
    path = _write_login(directory, expires_in=-60)
    state_path = _write_state(directory)

    assert _renewer(path).renew() is True

    assert len(received) == 1
    assert received[0]["path"] == "/oauth2/token"
    assert received[0]["form"] == {
        "grant_type": "refresh_token",
        "refresh_token": "r-old",
        "client_id": "mcode-public",
        "scope": "agent.default",
        "audience": "agent-backend",
    }
    assert received[0]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"

    record = _record(path)
    assert record["accessToken"] == "a-new"
    assert record["refreshToken"] == "r-new"
    assert record["generation"] == 7  # untouched, so the CLI still reads the login
    assert _read(state_path)["generation"] == 7


def test_no_test_reaches_the_real_account_service():
    """The default endpoints are the provider's; every test that calls one overrides the host."""
    assert mcr.TOKEN_ENDPOINT_BY_REGION == {
        "cn": "https://account.minimax.cn/oauth2/token",
        "en": "https://account.minimax.io/oauth2/token",
    }
    assert all(
        url.startswith("https://account.minimax.") for url in mcr.TOKEN_ENDPOINT_BY_REGION.values()
    )
