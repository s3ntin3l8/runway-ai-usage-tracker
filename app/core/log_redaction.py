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
