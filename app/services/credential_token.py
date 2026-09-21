"""Short-lived server-issued credential tokens for sidecar redemption.

The sidecar learns about ``provider_configs`` rows via the public
``/fleet/config`` endpoint (no secrets on the wire). For each
``(provider_id, account_id)`` row, ``/fleet/config`` embeds a signed
``credential_token`` that the sidecar redeems via
``POST /api/v1/fleet/credentials/redeem`` to fetch the actual decrypted
credentials (api_key, session_cookie, oai_sc_cookie).

Token format (``base64url`` of):

  ``"<json_payload>.<hex_hmac>"``

The HMAC is computed over the JSON payload using the server's
``INGEST_API_KEY`` — the same secret the sidecar uses for the ingest
endpoint — so a compromised token alone (without the HMAC secret) is
useless, and a leaked HMAC secret without a token is also useless.
Both layers are required at the redeem endpoint.

Tokens are time-limited (TTL configurable via ``CREDENTIAL_TOKEN_TTL_SECONDS``
on the server). The sidecar is expected to refetch them on its normal
heartbeat cadence; a stolen token has a short blast radius.

The module is the stdlib-only ``app/`` half of the pair. The matching
half in ``scripts/sidecar_pkg/credentials.py`` decodes and verifies the
same token format on the sidecar (the sidecar only needs to *issue*
equivalent tokens to itself for cache identity pinning; it doesn't
verify server-issued tokens — those are opaque references redeemed via
the server endpoint).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

# Bumped when the token format changes in a backwards-incompatible way.
# The redeem endpoint refuses tokens that don't carry the current version,
# forcing a re-issue when the format changes.
TOKEN_VERSION = 1


class CredentialTokenError(ValueError):
    """Raised when a credential token is malformed, expired, or unverifiable."""


@dataclass(frozen=True)
class CredentialTokenClaims:
    """Decoded claim set carried by a credential token."""

    provider_id: str
    account_id: str
    exp: int  # unix epoch seconds
    version: int

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.exp


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + padding).encode("ascii"))


def issue_credential_token(
    secret: str,
    *,
    provider_id: str,
    account_id: str,
    ttl_seconds: int,
    now: float | None = None,
) -> str:
    """Issue a signed token scoped to ``(provider_id, account_id)``.

    Args:
        secret: HMAC secret. Must match what the redeem endpoint uses for
            verification (in practice, the server's ``INGEST_API_KEY``).
        provider_id: Provider the token redeems credentials for.
        account_id: Account within ``provider_id`` the token redeems.
        ttl_seconds: Time-to-live for the token. ``0`` or negative values
            raise ``CredentialTokenError`` (use a non-zero TTL in
            practice; tests may opt into short windows).
        now: Override the current time (epoch seconds). Tests use this to
            generate tokens deterministically.

    Returns:
        The compact ``base64url(json_payload).hex_hmac`` string.

    Raises:
        CredentialTokenError: When ``ttl_seconds`` is non-positive or the
            inputs are otherwise unsuitable for encoding.
    """
    if ttl_seconds <= 0:
        raise CredentialTokenError(f"ttl_seconds must be positive, got {ttl_seconds}")
    if not provider_id or not account_id:
        raise CredentialTokenError("provider_id and account_id are required and must be non-empty")
    if not secret:
        raise CredentialTokenError("HMAC secret is required")

    ts = now if now is not None else time.time()
    payload: dict[str, Any] = {
        "v": TOKEN_VERSION,
        "pid": provider_id,
        "aid": account_id,
        "exp": int(ts + ttl_seconds),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    encoded_payload = _b64url_encode(payload_bytes)
    return f"{encoded_payload}.{signature}"


def verify_credential_token(
    secret: str,
    token: str,
    *,
    now: float | None = None,
) -> CredentialTokenClaims:
    """Verify a token's HMAC signature and return its claims.

    Args:
        secret: HMAC secret. Must match what the issuer used.
        token: The compact ``base64url(json_payload).hex_hmac`` string.
        now: Override the current time. Tests use this to verify tokens
            that *should* be expired.

    Returns:
        The decoded :class:`CredentialTokenClaims`.

    Raises:
        CredentialTokenError: When the token is malformed, has an
            unknown version, fails HMAC verification, or is expired.
    """
    if not token or not isinstance(token, str):
        raise CredentialTokenError("token is required and must be a string")
    if "." not in token:
        raise CredentialTokenError("token is malformed (missing separator)")
    encoded_payload, _, provided_signature = token.rpartition(".")
    if not encoded_payload or not provided_signature:
        raise CredentialTokenError("token is malformed (empty payload or signature)")
    # Tokens are ASCII on the wire. Reject non-ASCII up front so we don't
    # feed ``str`` into ``hmac.compare_digest`` (which raises ``TypeError``
    # rather than returning False, leaking 500s where callers expect
    # ``CredentialTokenError``).
    try:
        provided_signature.encode("ascii")
        encoded_payload.encode("ascii")
        secret.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CredentialTokenError("token contains non-ASCII characters") from exc

    try:
        payload_bytes = _b64url_decode(encoded_payload)
    except Exception as exc:
        raise CredentialTokenError(f"token payload is not valid base64url: {exc}") from exc

    try:
        payload = json.loads(payload_bytes)
    except Exception as exc:
        raise CredentialTokenError(f"token payload is not valid JSON: {exc}") from exc

    expected_signature = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise CredentialTokenError("token signature mismatch")

    version = payload.get("v")
    provider_id = payload.get("pid")
    account_id = payload.get("aid")
    exp = payload.get("exp")
    if version != TOKEN_VERSION:
        raise CredentialTokenError(
            f"token version {version!r} not supported (expected {TOKEN_VERSION})"
        )
    if not isinstance(provider_id, str) or not isinstance(account_id, str):
        raise CredentialTokenError("token missing provider_id or account_id")
    if not isinstance(exp, int):
        raise CredentialTokenError("token missing integer exp")

    claims = CredentialTokenClaims(
        provider_id=provider_id,
        account_id=account_id,
        exp=exp,
        version=version,
    )
    if claims.is_expired(now=now):
        raise CredentialTokenError("token expired")
    return claims
