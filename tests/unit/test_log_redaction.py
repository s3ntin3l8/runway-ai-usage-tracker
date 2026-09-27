"""PII redaction in the global exception handler (audit R10).

Account IDs in this codebase are email addresses (see
account_identity.resolve_account_id). Any uncaught exception whose
traceback formats a value involving an account_id thus emits an email
to whatever sink the log handlers go to. The audit's R10 calls for the
formatted exception text to be redacted before logging.
"""

from __future__ import annotations

from app.core.log_redaction import scrub_pii


def test_scrubs_a_plain_email_address():
    assert scrub_pii("user@example.com hit a 500") == "[REDACTED_EMAIL] hit a 500"


def test_scrubs_multiple_emails_in_one_string():
    text = "alice@example.com and bob+work@sub.example.co.uk"
    out = scrub_pii(text)
    assert "alice@example.com" not in out
    assert "bob+work@sub.example.co.uk" not in out
    assert out.count("[REDACTED_EMAIL]") == 2


def test_passes_unrelated_strings_through_unchanged():
    text = "no PII here, just a stack frame"
    assert scrub_pii(text) == text


def test_handles_non_string_input_gracefully():
    # The exception handler may pass through non-string values (None,
    # ints, etc) when stringifying loggers. Helper must not crash.
    assert scrub_pii(None) is None
    assert scrub_pii(42) == 42


# --- redact_secrets / redact_url (debug/raw captures) ------------------------

import httpx  # noqa: E402

from app.core.log_redaction import redact_secrets, redact_url  # noqa: E402

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"  # pragma: allowlist secret


def test_redacts_token_shapes_in_strings():
    text = f"jwt {_JWT} Bearer abcdefgh12345678 key sk-ant-api03-abcdefghijklmnop sessionKey=abc123"
    out = redact_secrets(text)
    assert isinstance(out, str)
    for leaked in (_JWT, "abcdefgh12345678", "sk-ant-api03-abcdefghijklmnop", "abc123"):
        assert leaked not in out
    assert "sessionKey=[REDACTED]" in out


def test_redacts_sensitive_keys_and_emails_recursively():
    body = {
        "access_token": "opaque-value",
        "user": {"email": "a@example.com", "session_id": "xyz"},
        "items": [{"api_key": "k"}, "contact bob@example.org"],
    }
    out = redact_secrets(body)
    assert out == {
        "access_token": "[REDACTED]",
        "user": {"email": "[REDACTED_EMAIL]", "session_id": "[REDACTED]"},
        "items": [{"api_key": "[REDACTED]"}, "contact [REDACTED_EMAIL]"],
    }
    assert body["access_token"] == "opaque-value"  # input not mutated


def test_numeric_token_counters_survive():
    body = {"input_tokens": 12, "max_tokens": 4096, "total_tokens": 99, "ok": True, "x": None}
    assert redact_secrets(body) == body


def test_redacts_query_secrets_embedded_in_a_larger_string():
    # e.g. an httpx exception message that includes the failing request URL.
    text = "connect to https://api.example.com/v1?key=SECRETVAL&page=2 timed out"
    out = redact_secrets(text)
    assert "SECRETVAL" not in out
    assert "page=2" in out


def test_redacts_sensitive_key_with_list_or_dict_value():
    body = {"access_token": ["opaque"], "session": {"id": "opaque"}}
    out = redact_secrets(body)
    assert out == {"access_token": "[REDACTED]", "session": "[REDACTED]"}


def test_sensitive_key_matches_hyphenated_api_key():
    assert redact_secrets({"api-key": "abc"}) == {"api-key": "[REDACTED]"}


def test_redacts_opencode_and_github_token_shapes():
    text = "oc_sk_abcdefgh12345678 ghp_abcdefghijklmnopqrst01 github_pat_abcdefghijklmnopqrst01"
    out = redact_secrets(text)
    for leaked in ("oc_sk_abcdefgh12345678", "ghp_abcdefghijklmnopqrst01"):
        assert leaked not in out


def test_redact_url_strips_userinfo_and_adds_client_secret_code():
    out = redact_url(
        "https://user:hunter2@api.example.com/oauth?client_secret=verysecret&code=authcode"  # pragma: allowlist secret
    )
    assert "hunter2" not in out
    assert "user:" not in out
    assert "verysecret" not in out
    assert "authcode" not in out
    assert "client_secret=[REDACTED]" in out
    assert "code=[REDACTED]" in out


def test_redact_url_masks_sensitive_query_params():
    out = redact_url("https://api.example.com/v1/x?key=AIzaSECRET&page=2")
    assert "AIzaSECRET" not in out
    assert "page=2" in out


# --- capture helpers ---------------------------------------------------------


def test_capture_response_entry_redacts_body_headers_and_url():
    from app.api.endpoints.system import _capture_response_entry

    req = httpx.Request("GET", "https://api.example.com/u?token=SECRETVAL")
    resp = httpx.Response(
        200,
        json={"access_token": "abc", "email": "me@example.com", "jwt": _JWT, "input_tokens": 5},
        headers={"Set-Cookie": "sid=1", "X-Trace": f"Bearer {_JWT}"},
        request=req,
    )
    entry = _capture_response_entry(resp)
    assert entry["body"]["access_token"] == "[REDACTED]"
    assert entry["body"]["email"] == "[REDACTED_EMAIL]"
    assert _JWT not in str(entry)
    assert entry["body"]["input_tokens"] == 5
    assert entry["headers"]["set-cookie"] == "[MASKED]"
    assert "SECRETVAL" not in entry["url"]


def test_capture_response_entry_masks_activity_session_header():
    from app.api.endpoints.system import _capture_response_entry

    req = httpx.Request("GET", "https://api.example.com/u")
    resp = httpx.Response(
        200,
        json={},
        headers={"x-activity-session-id": "opaque-session-value"},
        request=req,
    )
    entry = _capture_response_entry(resp)
    assert entry["headers"]["x-activity-session-id"] == "[MASKED]"


def test_capture_response_entry_redacts_non_json_body():
    from app.api.endpoints.system import _capture_response_entry

    req = httpx.Request("GET", "https://api.example.com/x")
    resp = httpx.Response(401, text=f"bad token {_JWT}", request=req)
    assert _JWT not in _capture_response_entry(resp)["body"]
