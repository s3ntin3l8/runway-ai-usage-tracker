"""PII redaction for log lines and formatted exceptions.

Closes audit finding R10. Account IDs in this codebase are email
addresses (see `app.services.account_identity.resolve_account_id`).
Any uncaught exception whose traceback formats a value involving an
account_id thus emits an email to the configured log sink.

The regex deliberately favours conservative matches — it strips
recognisable email-shaped substrings and leaves everything else alone.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# RFC-5321-ish: liberal enough to catch the email shapes Runway actually
# uses (provider-issued addresses); strict enough not to claim every
# `@` is an email. Two-char-minimum TLD avoids matching '@'-prefixed
# log markers that happen to have a dot afterwards.
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_REPLACEMENT = "[REDACTED_EMAIL]"


def scrub_pii(value: object) -> object:
    """Return `value` with email-shaped substrings replaced.

    Non-string input is passed through unchanged — the helper is used
    by paths that may also handle Nones, ints, and exception objects.
    """
    if not isinstance(value, str):
        return value
    return _EMAIL_PATTERN.sub(_REPLACEMENT, value)


# --- Credential redaction for debug captures --------------------------------

_SECRET_REPLACEMENT = "[REDACTED]"

# Token-shaped substrings that can appear inside any string value.
_SECRET_STRING_PATTERNS: tuple[re.Pattern[str], ...] = (
    # JWTs (header.payload[.signature])
    re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*"),
    # Authorization-style bearer values
    re.compile(r"(?i)\bbearer\s+[\w.~+/=-]{8,}"),
    # Provider API keys / session ids (sk-ant-*, sk-proj-*, sk-or-*, ...)
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    # Cookie-style session pairs
    re.compile(r"(?i)\b(sessionKey|session_id|sessionid|sid)=[^;\s\"'&]+"),
)

# Dict keys whose *string* values are secrets. Numeric fields such as
# ``input_tokens`` / ``max_tokens`` are deliberately left alone.
_SENSITIVE_KEY = re.compile(
    r"(?i)(^|_|-)(access_?token|refresh_?token|id_?token|token|secret|password|passwd|"
    r"api_?key|apikey|authorization|cookie|session[\w-]*|credential[s]?|private_?key|csrf[\w-]*)$"
)

_SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "key",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "id_token",
        "sig",
        "signature",
        "secret",
        "password",
        "session",
        "sessionkey",
    }
)


def _redact_string(value: str) -> str:
    for pattern in _SECRET_STRING_PATTERNS:
        value = pattern.sub(_substitute, value)
    return str(scrub_pii(value))


def _substitute(match: re.Match[str]) -> str:
    # Keep the "sessionKey=" style prefix so the shape stays readable.
    if match.lastindex:
        return f"{match.group(1)}={_SECRET_REPLACEMENT}"
    return _SECRET_REPLACEMENT


def redact_secrets(value: object) -> object:
    """Return a copy of *value* with credential-shaped content redacted.

    Recurses through dicts/lists; strings are scrubbed for token shapes and
    emails, and string values under sensitive-looking keys are replaced whole.
    Non-string scalars (ints, floats, bools, None) pass through, so usage
    counters keep their values. The input is never mutated.
    """
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        out: dict[object, object] = {}
        for k, v in value.items():
            if isinstance(k, str) and isinstance(v, str) and _SENSITIVE_KEY.search(k):
                out[k] = _SECRET_REPLACEMENT
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value


def redact_url(url: str) -> str:
    """Redact sensitive query-param values and token-shaped substrings in *url*."""
    parts = urlsplit(url)
    if parts.query:
        pairs = [
            (k, _SECRET_REPLACEMENT if k.lower() in _SENSITIVE_QUERY_PARAMS else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        url = urlunsplit(parts._replace(query=urlencode(pairs, safe="[]")))
    return _redact_string(url)
