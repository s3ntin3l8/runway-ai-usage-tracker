"""Strict version-string parsing, shared by the sidecar and the server.

A CLI version can end up in an HTTP header — the Antigravity User-Agent's
version decides which quota pools the Cloud Code Assist API serves — so the
value has to be plain ASCII digits. httpx encodes headers as ASCII and raises
on anything else, and a version that arrives over the ingest route is
attacker-influenced input until it is vetted.
"""

import re

# ASCII digits only. Python's ``\d`` also matches Unicode decimals (e.g. the
# Arabic-Indic "٤"), which would reach the header as non-ASCII bytes.
_VERSION_RE = re.compile(r"^v?([0-9]+(?:\.[0-9]+){0,2})$")


def normalize_version(text: object) -> str | None:
    """The dotted version in *text*, or None when it is not one.

    Accepts an optional leading "v" and one to three numeric parts
    ("v1.4.3" → "1.4.3"). Everything else is None — including a non-string
    and a version carrying non-ASCII digits, both of which would break the
    header it ends up in.
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    return match.group(1) if match else None
