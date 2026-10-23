"""Decode values stored by go-keyring, the library the Antigravity CLI keeps its token with.

The macOS Keychain entry the ``agy`` CLI writes holds an envelope, not the
credential itself: ``go-keyring-base64:`` followed by base64, or
``go-keyring-encoded:`` followed by hex for a binary secret. Mirroring the
writer keeps the reader honest - an envelope we do not recognise yields no
credential rather than a wrong one.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

BASE64_PREFIX = "go-keyring-base64:"
HEX_PREFIX = "go-keyring-encoded:"


def decode_envelope(raw: str) -> str | None:
    """Return the plaintext a go-keyring store holds, or None when unusable.

    A value without a known envelope is returned unchanged - go-keyring keeps
    plain strings as they are - so a future writer that stops wrapping the
    secret still reads.
    """
    text = raw.strip()
    if text.startswith(BASE64_PREFIX):
        payload = text[len(BASE64_PREFIX) :]
        try:
            return base64.b64decode(payload, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
    if text.startswith(HEX_PREFIX):
        payload = text[len(HEX_PREFIX) :]
        try:
            return bytes.fromhex(payload).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return text


def decode_document(raw: str) -> dict[str, Any] | None:
    """The envelope-decoded credential document, or None when it is not usable."""
    plaintext = decode_envelope(raw)
    if plaintext is None:
        return None
    try:
        document = json.loads(plaintext)
    except (ValueError, TypeError):
        return None
    return document if isinstance(document, dict) else None
