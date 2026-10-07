"""AnthropicRenewer: refresh the Claude Code login and write it back to its credentials file."""

import io
import json
import logging
import os
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from app.services import token_refresher
from scripts.sidecar_pkg import anthropic_renewer as ar
from scripts.sidecar_pkg import keep_alive
from scripts.sidecar_pkg.anthropic_renewer import AnthropicRenewer, RefreshRejectedError

SCOPES = [
    "user:file_upload",
    "user:inference",
    "user:mcp_servers",
    "user:plugins",
    "user:profile",
    "user:sessions:claude_code",
]


def _creds(
    path: Path,
    expires_in: float,
    refresh: str = "r-old",
    *,
    age: float = 600,
    scopes: list[str] | None = None,
) -> Path:
    """A Claude Code credentials file (mode 0600), last written ``age`` seconds ago."""
    path.parent.mkdir(parents=True, exist_ok=True)
    oauth: dict = {
        "accessToken": "a-old",
        "refreshToken": refresh,
        "expiresAt": int((time.time() + expires_in) * 1000),
        "refreshTokenExpiresAt": int((time.time() + 86400 * 20) * 1000),
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_5x",
    }
    if scopes is not False:
        oauth["scopes"] = SCOPES if scopes is None else scopes
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": oauth,
                "mcpOAuth": {"srv|abc": {"serverName": "srv", "accessToken": "mcp-secret"}},
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
        "access_token": "a-new",
        "refresh_token": "r-new",
        "expires_in": 28800,
        "refresh_token_expires_in": 2_445_000,
        "scope": " ".join(SCOPES),
        "token_type": "Bearer",
        "account": {"uuid": "x"},
    }
    base.update(over)
    return base


def _respond(monkeypatch, data=None, error=None):
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake(refresh_token, scopes=()):
        calls.append((refresh_token, tuple(scopes)))
        if error:
            raise error
        return data if data is not None else _response()

    monkeypatch.setattr(ar, "request_refresh", fake)
    return calls


def _renewer(*paths: Path) -> AnthropicRenewer:
    return AnthropicRenewer(lambda: list(paths))


def test_constants_match_the_server_refresher():
    assert token_refresher._REFRESH_ENDPOINTS["anthropic"] == ar.TOKEN_ENDPOINT
    assert token_refresher._PROVIDER_CLIENT_IDS["anthropic"] == ar.CLIENT_ID


def test_the_renewer_is_named_after_its_provider():
    from app.services.refresh_policy import KEEP_ALIVE_PROVIDERS

    assert AnthropicRenewer.name == "anthropic"
    assert AnthropicRenewer.name in KEEP_ALIVE_PROVIDERS


class TestDue:
    def test_not_due_while_the_token_is_comfortably_valid(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", 4 * 3600)
        assert _renewer(f).due() is False

    def test_due_inside_the_lead_window(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", 5 * 60)
        assert _renewer(f).due() is True

    def test_due_once_lapsed(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", -3600)
        assert _renewer(f).due() is True

    def test_due_when_the_expiry_is_missing(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", 3600)
        data = _read(f)
        del data["claudeAiOauth"]["expiresAt"]
        f.write_text(json.dumps(data))
        os.utime(f, (time.time() - 600, time.time() - 600))
        assert _renewer(f).due() is True

    def test_nothing_to_renew_without_a_file_an_entry_or_a_refresh_token(self, tmp_path):
        no_entry = tmp_path / "a.json"
        no_entry.write_text(json.dumps({"mcpOAuth": {}}))
        blank = _creds(tmp_path / "b.json", -10, refresh="")
        junk = tmp_path / "c.json"
        junk.write_text("{not json")
        r = _renewer(tmp_path / "missing.json", no_entry, blank, junk)
        assert r.due() is False
        assert r.renew() is True  # nothing due is not a failure

    def test_a_file_written_a_moment_ago_is_left_alone(self, tmp_path):
        """Claude Code (or another renewer) just wrote it: don't race it for a single-use token."""
        f = _creds(tmp_path / ".credentials.json", -10, age=5)
        assert _renewer(f).due() is False
        os.utime(f, (time.time() - 60, time.time() - 60))
        assert _renewer(f).due() is True

    def test_login_due_boundaries(self):
        login = ar.Login(Path("x"), "r", expires_ms=1_000_000.0, scopes=())
        assert ar.login_due(login, now=1000 - 900 - 1) is False
        assert ar.login_due(login, now=1000 - 900) is True
        assert ar.login_due(login, now=2000) is True


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
            seen["timeout"] = timeout
            if isinstance(result, Exception):
                raise result
            return _FakeResponse(result)

        monkeypatch.setattr(ar.urllib.request, "urlopen", fake)
        return seen

    def test_sends_exactly_claude_codes_request(self, monkeypatch):
        """JSON body that includes ``scope`` and no header but Content-Type: the form-encoded
        body with extra User-Agent/anthropic-beta headers was answered with 429 every time."""
        seen = self._patch(monkeypatch, b'{"access_token": "n", "refresh_token": "r2"}')
        out = ar.request_refresh("old-refresh", ("user:profile", "user:inference"))
        assert out["access_token"] == "n"
        req = seen["req"]
        assert req.full_url == "https://platform.claude.com/v1/oauth/token"
        assert req.get_method() == "POST"
        assert json.loads(req.data) == {
            "grant_type": "refresh_token",
            "refresh_token": "old-refresh",
            "client_id": ar.CLIENT_ID,
            "scope": "user:profile user:inference",
        }
        headers = {k.lower(): v for k, v in req.header_items()}
        assert headers["content-type"] == "application/json"
        assert "anthropic-beta" not in headers
        assert headers["user-agent"] == ar.USER_AGENT
        assert set(headers) == {"content-type", "user-agent"}

    def test_omits_scope_when_the_file_has_none(self, monkeypatch):
        """No scope sent -> the endpoint grants the login's default set (verified live); an
        explicit list could be rejected for a login that was granted fewer."""
        seen = self._patch(monkeypatch, b'{"access_token": "n"}')
        ar.request_refresh("r")
        assert "scope" not in json.loads(seen["req"].data)
        assert not hasattr(ar, "DEFAULT_SCOPES")

    @pytest.mark.parametrize("code", [400, 401, 403])
    def test_a_dead_or_used_token_is_a_rejection(self, monkeypatch, code):
        self._patch(
            monkeypatch,
            _http_error(
                code,
                b'{"error": "invalid_grant", "error_description": "Refresh token not found or invalid"}',
            ),
        )
        with pytest.raises(RefreshRejectedError, match="invalid_grant"):
            ar.request_refresh("r")

    @pytest.mark.parametrize("code", [408, 429, 500, 503])
    def test_throttling_and_server_errors_are_transient(self, monkeypatch, code):
        body = b'{"error": {"type": "rate_limit_error", "message": "Rate limited."}}'
        self._patch(monkeypatch, _http_error(code, body))
        with pytest.raises(OSError) as exc:
            ar.request_refresh("r")
        assert not isinstance(exc.value, RefreshRejectedError)

    def test_a_nested_error_object_is_read_for_the_diagnosis(self, monkeypatch):
        self._patch(monkeypatch, _http_error(400, b'{"error": {"type": "invalid_request_error"}}'))
        with pytest.raises(RefreshRejectedError, match="invalid_request_error"):
            ar.request_refresh("r")

    def test_an_unreadable_error_body_still_rejects(self, monkeypatch):
        self._patch(monkeypatch, _http_error(400, b"<html>"))
        with pytest.raises(RefreshRejectedError, match="HTTP 400"):
            ar.request_refresh("r")

    @pytest.mark.parametrize("body", [b"not json", b"[]", b'{"nope": 1}'])
    def test_bad_success_bodies_are_errors(self, monkeypatch, body):
        self._patch(monkeypatch, body)
        with pytest.raises(OSError):
            ar.request_refresh("r")

    def test_network_failure_is_an_oserror(self, monkeypatch):
        self._patch(monkeypatch, OSError("down"))
        with pytest.raises(OSError):
            ar.request_refresh("r")


class TestWriteBack:
    def test_updates_only_the_oauth_tokens_and_keeps_the_rest_of_the_document(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", -10)
        before = _read(f)
        login = ar.read_login(f)
        assert ar.write_back(login, _response()) == "written"

        after = _read(f)
        oauth = after["claudeAiOauth"]
        assert (oauth["accessToken"], oauth["refreshToken"]) == ("a-new", "r-new")
        assert after["mcpOAuth"] == before["mcpOAuth"]  # Claude Code also writes this block
        assert after["custom"] == "kept"
        assert (
            oauth["subscriptionType"] == "max" and oauth["rateLimitTier"] == "default_claude_max_5x"
        )
        assert set(after) == set(before)
        assert set(oauth) == set(before["claudeAiOauth"])

    def test_sets_both_expiries_from_the_response(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", -10)
        before = time.time()
        ar.write_back(
            ar.read_login(f), _response(expires_in=28800, refresh_token_expires_in=2_445_000)
        )
        oauth = _read(f)["claudeAiOauth"]
        assert abs(oauth["expiresAt"] / 1000 - (before + 28800)) < 5
        assert abs(oauth["refreshTokenExpiresAt"] / 1000 - (before + 2_445_000)) < 5

    def test_keeps_the_refresh_expiry_and_scopes_when_the_response_has_none(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", -10)
        before = _read(f)["claudeAiOauth"]
        resp = _response()
        del resp["refresh_token_expires_in"], resp["scope"]
        ar.write_back(ar.read_login(f), resp)
        oauth = _read(f)["claudeAiOauth"]
        assert oauth["refreshTokenExpiresAt"] == before["refreshTokenExpiresAt"]
        assert oauth["scopes"] == before["scopes"]

    def test_takes_the_scopes_the_endpoint_granted(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", -10, scopes=["user:profile"])
        ar.write_back(ar.read_login(f), _response(scope="user:profile user:inference"))
        assert _read(f)["claudeAiOauth"]["scopes"] == ["user:profile", "user:inference"]

    def test_always_keeps_the_new_refresh_token_because_the_old_one_is_spent(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", -10)
        resp = _response()
        del resp["refresh_token"]  # never seen, but never lose a token over it
        assert ar.write_back(ar.read_login(f), resp) == "written"
        assert _read(f)["claudeAiOauth"]["refreshToken"] == "r-old"

    def test_a_response_without_expires_in_gets_a_short_expiry_so_we_look_again(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", -10)
        resp = _response()
        del resp["expires_in"]
        ar.write_back(ar.read_login(f), resp)
        left = _read(f)["claudeAiOauth"]["expiresAt"] / 1000 - time.time()
        assert 0 < left <= ar.FALLBACK_EXPIRES_IN + 5

    def test_keeps_the_file_mode_and_leaves_no_temp_file(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", -10)
        ar.write_back(ar.read_login(f), _response())
        assert oct(f.stat().st_mode & 0o777) == "0o600"
        assert [p.name for p in tmp_path.iterdir()] == [".credentials.json"]

    def test_the_cli_renewing_first_wins(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", -10)
        login = ar.read_login(f)
        _creds(f, 8 * 3600, refresh="r-cli")  # Claude Code refreshed meanwhile
        assert ar.write_back(login, _response()) == "superseded"
        assert _read(f)["claudeAiOauth"]["refreshToken"] == "r-cli"

    def test_a_vanished_file_or_entry_is_not_a_crash(self, tmp_path):
        f = _creds(tmp_path / ".credentials.json", -10)
        login = ar.read_login(f)
        f.write_text(json.dumps({"mcpOAuth": {}}))
        assert ar.write_back(login, _response()) == "superseded"
        f.unlink()
        assert ar.write_back(login, _response()) == "failed"

    def test_a_failed_write_cleans_up_after_itself(self, tmp_path, monkeypatch):
        f = _creds(tmp_path / ".credentials.json", -10)
        login = ar.read_login(f)

        def boom(*_a):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        assert ar.write_back(login, _response()) == "failed"
        assert [p.name for p in tmp_path.iterdir()] == [".credentials.json"]
        assert _read(f)["claudeAiOauth"]["refreshToken"] == "r-old"

    def test_a_symlinked_credentials_file_stays_a_symlink(self, tmp_path):
        real = _creds(tmp_path / "dotfiles" / "credentials.json", -10)
        link = tmp_path / ".claude" / ".credentials.json"
        link.parent.mkdir()
        link.symlink_to(real)
        os.utime(real, (time.time() - 600, time.time() - 600))
        assert ar.write_back(ar.read_login(link), _response()) == "written"
        assert link.is_symlink()
        assert _read(real)["claudeAiOauth"]["refreshToken"] == "r-new"


class TestRenew:
    def test_renews_a_due_login_and_writes_it_back(self, tmp_path, monkeypatch):
        f = _creds(tmp_path / ".credentials.json", -10)
        calls = _respond(monkeypatch)
        assert _renewer(f).renew() is True
        assert calls == [("r-old", tuple(SCOPES))]  # the file's own scopes are requested
        assert _read(f)["claudeAiOauth"]["refreshToken"] == "r-new"

    def test_requests_the_default_scopes_for_a_login_without_scopes(self, tmp_path, monkeypatch):
        f = _creds(tmp_path / ".credentials.json", -10, scopes=[])
        calls = _respond(monkeypatch)
        _renewer(f).renew()
        assert calls == [("r-old", ())]  # no scopes known -> request_refresh sends none

    def test_each_login_is_renewed_independently(self, tmp_path, monkeypatch):
        a = _creds(tmp_path / "a" / ".credentials.json", -10, refresh="ra")
        b = _creds(tmp_path / "b" / ".credentials.json", -10, refresh="rb")
        fresh = _creds(tmp_path / "c" / ".credentials.json", 6 * 3600, refresh="rc")
        calls = _respond(monkeypatch)
        assert _renewer(a, b, fresh).renew() is True
        assert sorted(c[0] for c in calls) == ["ra", "rb"]
        assert _read(fresh)["claudeAiOauth"]["refreshToken"] == "rc"

    def test_a_file_reachable_through_two_paths_is_renewed_once(self, tmp_path, monkeypatch):
        real = _creds(tmp_path / "real" / ".credentials.json", -10)
        link = tmp_path / "link.json"
        link.symlink_to(real)
        calls = _respond(monkeypatch)
        _renewer(real, link).renew()
        assert len(calls) == 1

    def test_targets_are_re_evaluated_on_every_tick(self, tmp_path, monkeypatch):
        first = _creds(tmp_path / "one" / ".credentials.json", 6 * 3600)
        second = _creds(tmp_path / "two" / ".credentials.json", -10)
        current = [first]
        r = AnthropicRenewer(lambda: list(current))
        assert r.due() is False
        current.append(second)  # a config reload added a login dir
        assert r.due() is True
        calls = _respond(monkeypatch)
        r.renew()
        assert len(calls) == 1

    def test_a_rejection_after_the_cli_renewed_first_is_not_a_logout(
        self, tmp_path, monkeypatch, caplog
    ):
        """Tokens are single-use: if Claude Code refreshed between our read and our request, we
        get invalid_grant — but the file already holds its newer token, so all is well."""
        f = _creds(tmp_path / ".credentials.json", -10)

        def cli_renews_first(refresh_token, scopes=()):
            _creds(f, 8 * 3600, refresh="r-cli", age=2)
            raise RefreshRejectedError("HTTP 400 invalid_grant")

        monkeypatch.setattr(ar, "request_refresh", cli_renews_first)
        r = _renewer(f)
        with caplog.at_level(logging.INFO):
            assert r.renew() is True
        assert "renewed by Claude Code first" in caplog.text
        assert r._rejected == {}
        assert _read(f)["claudeAiOauth"]["refreshToken"] == "r-cli"

    def test_a_rejection_with_an_unchanged_file_is_a_dead_login(
        self, tmp_path, monkeypatch, caplog
    ):
        f = _creds(tmp_path / ".credentials.json", -10, refresh="SECRET-REFRESH")
        before = f.read_text()
        calls = _respond(monkeypatch, error=RefreshRejectedError("HTTP 400 invalid_grant"))
        r = _renewer(f)
        with caplog.at_level(logging.INFO):
            assert r.renew() is False
        assert f.read_text() == before
        assert "claude auth login" in caplog.text and "invalid_grant" in caplog.text
        assert "SECRET-REFRESH" not in caplog.text
        # Not retried with the same dead token…
        assert r.due() is False
        r.renew()
        assert len(calls) == 1
        # …until the user logs in again (a new refresh token lifts the block).
        _creds(f, -10, refresh="fresh-login")
        assert r.due() is True

    @pytest.mark.parametrize("code", [429, 500])
    def test_throttling_is_transient_so_nothing_is_blocked_or_written(
        self, tmp_path, monkeypatch, code
    ):
        f = _creds(tmp_path / ".credentials.json", -10)
        before = f.read_text()
        _respond(monkeypatch, error=OSError(f"HTTP {code}"))
        r = _renewer(f)
        assert r.renew() is False
        assert f.read_text() == before
        assert r.due() is True and r._rejected == {}

    def test_a_failed_save_is_reported_as_a_failure(self, tmp_path, monkeypatch, caplog):
        f = _creds(tmp_path / ".credentials.json", -10)
        _respond(monkeypatch)
        monkeypatch.setattr(ar, "atomic_replace_json", lambda *a, **k: False)
        with caplog.at_level(logging.WARNING):
            assert _renewer(f).renew() is False
        assert "could not write the renewed login" in caplog.text

    def test_a_superseded_write_counts_as_success(self, tmp_path, monkeypatch):
        f = _creds(tmp_path / ".credentials.json", -10)
        _respond(monkeypatch)
        monkeypatch.setattr(ar, "write_back", lambda login, resp: "superseded")
        assert _renewer(f).renew() is True

    def test_never_logs_token_material(self, tmp_path, monkeypatch, caplog):
        f = _creds(tmp_path / ".credentials.json", -10, refresh="R-SECRET-1")
        _respond(monkeypatch, data=_response(access_token="A-SECRET-2", refresh_token="R-SECRET-3"))
        with caplog.at_level(logging.DEBUG):
            _renewer(f).renew()
        for secret in ("R-SECRET-1", "A-SECRET-2", "R-SECRET-3", "mcp-secret"):
            assert secret not in caplog.text

    def test_concurrent_renewals_refresh_the_single_use_token_once(self, tmp_path, monkeypatch):
        """A keep-alive toggled off/on can leave two threads renewing; the second must find the
        login already renewed instead of spending the new token's predecessor again."""
        f = _creds(tmp_path / ".credentials.json", -10)
        calls: list[str] = []
        started, release = threading.Event(), threading.Event()

        def slow(refresh_token, scopes=()):
            calls.append(refresh_token)
            started.set()
            release.wait(5)
            return _response()

        monkeypatch.setattr(ar, "request_refresh", slow)
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
        assert _read(f)["claudeAiOauth"]["refreshToken"] == "r-new"


class TestKeepAliveThread:
    def test_a_failing_renewal_backs_off_that_renewer_only(self, tmp_path, monkeypatch):
        f = _creds(tmp_path / ".credentials.json", -10)
        monkeypatch.setattr(
            ar, "request_refresh", lambda *a, **k: (_ for _ in ()).throw(OSError("429"))
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
        assert "anthropic" in thread._renewer_resume_at  # backed off after the failure
        assert Other.runs == 2  # the healthy renewer is unaffected


class TestLostTokenSafety:
    """The old refresh token is spent the moment the endpoint answers: never lose the new one."""

    def test_a_failed_save_is_retried_without_spending_another_token(self, tmp_path, monkeypatch):
        f = _creds(tmp_path / ".credentials.json", -10)
        calls = _respond(monkeypatch)
        real = ar.atomic_replace_json
        monkeypatch.setattr(ar, "atomic_replace_json", lambda *a, **k: False)
        r = _renewer(f)
        assert r.renew() is False  # exchanged, but could not save
        assert len(calls) == 1 and _read(f)["claudeAiOauth"]["refreshToken"] == "r-old"

        monkeypatch.setattr(ar, "atomic_replace_json", real)  # the disk recovered
        assert r.renew() is True
        assert len(calls) == 1  # no second exchange: the stored response was saved
        assert _read(f)["claudeAiOauth"]["refreshToken"] == "r-new"
        assert r._unsaved == {}

    def test_a_stored_response_is_never_saved_over_a_newer_login(self, tmp_path, monkeypatch):
        f = _creds(tmp_path / ".credentials.json", -10)
        _respond(monkeypatch)
        monkeypatch.setattr(ar, "atomic_replace_json", lambda *a, **k: False)
        r = _renewer(f)
        r.renew()
        assert len(r._unsaved) == 1
        # Claude Code refreshed itself meanwhile: the file holds a newer login, not due yet.
        _creds(f, 8 * 3600, refresh="r-cli")
        assert r.due() is False
        assert _read(f)["claudeAiOauth"]["refreshToken"] == "r-cli"
        # Much later it is due again: the stale response is discarded and a fresh exchange is
        # made from the CLI's own token, never the old stored one.
        monkeypatch.undo()
        calls = _respond(monkeypatch)
        _creds(f, -10, refresh="r-cli")
        assert r.renew() is True
        assert calls == [("r-cli", tuple(SCOPES))]
        assert r._unsaved == {}

    @pytest.mark.parametrize("bad", ["soon", None, -5, 0])
    def test_an_unusable_expires_in_cannot_cost_us_the_new_tokens(self, tmp_path, bad):
        f = _creds(tmp_path / ".credentials.json", -10)
        assert ar.write_back(ar.read_login(f), _response(expires_in=bad)) == "written"
        oauth = _read(f)["claudeAiOauth"]
        assert oauth["refreshToken"] == "r-new"
        left = oauth["expiresAt"] / 1000 - time.time()
        assert 0 < left <= ar.FALLBACK_EXPIRES_IN + 5
