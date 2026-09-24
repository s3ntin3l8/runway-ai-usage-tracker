"""One-time sidecar pairing codes.

Flow (see docs/SECURITY.md → *Sidecar pairing*):

1. An admin clicks *Pair a sidecar* on the Fleet page →
   ``POST /api/v1/fleet/pairing-codes`` mints a random code, stores only its
   SHA-256, and returns ``runway-sidecar://pair?server=<url>&code=<code>``.
2. The link opens the sidecar, which shows a confirmation page naming the
   server; only after the user clicks *Pair* does it call
   ``POST /api/v1/fleet/pair`` with the code.
3. Redeem is single-use and time-boxed; it returns ``{api_url, api_key}``
   (the ingest HMAC key) over the same TLS channel the sidecar will use for
   ingest, so the key itself never appears in a URL, browser history or log.

Codes are 10 Crockford base32 characters (50 bits), rendered ``XXXXX-XXXXX``.
Combined with the 10/min/IP rate limit on redeem and a 10-minute TTL, blind
guessing is hopeless. Crockford's alphabet drops I/L/O/U, and ``normalize``
folds the common misreadings back, so a code typed by hand still works.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

from sqlalchemy import delete, update
from sqlmodel import Session, col, select

from app.models.db import SidecarPairingCode

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32  # pragma: allowlist secret
_CODE_LEN = 10
_FOLD = str.maketrans({"O": "0", "I": "1", "L": "1"})
DEEP_LINK_SCHEME = "runway-sidecar"
# Keep redeemed/expired rows around briefly for the audit trail.
_RETENTION = timedelta(days=1)


class PairingError(ValueError):
    """Invalid, expired or already-used pairing code (deliberately vague)."""


def generate_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    return f"{raw[:5]}-{raw[5:]}"


def normalize(code: str) -> str:
    """Canonical form used for hashing: uppercase, no separators, folded."""
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum()).translate(_FOLD)
    return cleaned


def hash_code(code: str) -> str:
    return hashlib.sha256(normalize(code).encode("ascii", "ignore")).hexdigest()


def valid_server_url(url: str) -> str | None:
    """``scheme://host[:port][/path]`` without query/fragment, or None."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def deep_link(server_url: str, code: str) -> str:
    return (
        f"{DEEP_LINK_SCHEME}://pair?server={quote(server_url, safe='')}&code={quote(code, safe='')}"
    )


def create_code(
    session: Session,
    *,
    server_url: str,
    ttl_seconds: int,
    created_by: str | None,
) -> tuple[str, datetime]:
    """Mint and persist a new code; returns ``(code, expires_at)``."""
    now = datetime.now(UTC)
    # Opportunistic sweep so the table never grows unbounded.
    session.exec(  # type: ignore[call-overload]
        delete(SidecarPairingCode).where(col(SidecarPairingCode.expires_at) < now - _RETENTION)
    )
    code = generate_code()
    expires_at = now + timedelta(seconds=ttl_seconds)
    session.add(
        SidecarPairingCode(
            code_hash=hash_code(code),
            server_url=server_url,
            expires_at=expires_at,
            created_by=created_by,
        )
    )
    session.commit()
    return code, expires_at


def redeem(session: Session, code: str, *, hostname: str | None) -> SidecarPairingCode:
    """Atomically mark *code* used and return its row.

    The conditional UPDATE (``used_at IS NULL AND expires_at > now``) is the
    single-use guarantee: two concurrent redeems of one code can't both win.
    Raises ``PairingError`` for unknown, expired or already-used codes alike.
    """
    if len(normalize(code)) != _CODE_LEN:
        raise PairingError("invalid or expired pairing code")
    now = datetime.now(UTC)
    digest = hash_code(code)
    result = session.exec(  # type: ignore[call-overload]
        update(SidecarPairingCode)
        .where(col(SidecarPairingCode.code_hash) == digest)
        .where(col(SidecarPairingCode.used_at).is_(None))
        .where(col(SidecarPairingCode.expires_at) > now)
        .values(used_at=now, used_by_hostname=(hostname or "")[:255] or None)
    )
    session.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise PairingError("invalid or expired pairing code")
    row = session.exec(
        select(SidecarPairingCode).where(col(SidecarPairingCode.code_hash) == digest)
    ).one()
    session.refresh(row)
    return row
