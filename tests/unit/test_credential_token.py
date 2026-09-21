"""Unit tests for ``app/services/credential_token.py``.

Token issuance and verification are HMAC-SHA256 over a JSON payload.
The round-trip tests below pin the contract the redeem endpoint relies
on: a token issued by ``issue_credential_token`` with a given secret
verifies cleanly via ``verify_credential_token``; a token tampered with
or signed by a different secret fails.
"""

from __future__ import annotations

import pytest

from app.services.credential_token import (
    TOKEN_VERSION,
    CredentialTokenClaims,
    CredentialTokenError,
    issue_credential_token,
    verify_credential_token,
)

SECRET = "test-secret-shared-with-sidecar"


def _fixed_now() -> float:
    """Return a fixed epoch (2026-01-01T00:00:00Z) for deterministic tests."""
    return 1767225600.0


def test_issue_and_verify_roundtrip():
    """A freshly-issued token verifies and yields the embedded claims."""
    token = issue_credential_token(
        SECRET,
        provider_id="anthropic",
        account_id="alice@example.com",
        ttl_seconds=3600,
        now=_fixed_now(),
    )
    claims = verify_credential_token(SECRET, token, now=_fixed_now())
    assert claims.provider_id == "anthropic"
    assert claims.account_id == "alice@example.com"
    assert claims.exp == _fixed_now() + 3600
    assert claims.version == TOKEN_VERSION


def test_token_format_is_compact_and_urlsafe():
    """The wire format must be safe to embed in JSON without escaping."""
    token = issue_credential_token(
        SECRET,
        provider_id="anthropic",
        account_id="alice@example.com",
        ttl_seconds=3600,
    )
    # One ``.`` separator (between payload and HMAC). No spaces, no quotes,
    # no JSON-special characters that would force escaping.
    assert token.count(".") == 1
    for ch in token:
        assert ch.isalnum() or ch in "-_."


def test_wrong_secret_fails():
    """A token issued under one secret cannot be verified with another."""
    token = issue_credential_token(
        SECRET, provider_id="anthropic", account_id="default", ttl_seconds=60
    )
    with pytest.raises(CredentialTokenError) as excinfo:
        verify_credential_token("different-secret", token)
    assert "signature" in str(excinfo.value).lower()


def test_tampered_payload_fails():
    """Tampering with the encoded payload invalidates the HMAC."""
    token = issue_credential_token(
        SECRET, provider_id="anthropic", account_id="alice@example.com", ttl_seconds=60
    )
    encoded, _, sig = token.rpartition(".")
    # Swap the last char of the payload (within base64url alphabet).
    tampered = encoded[:-1] + ("A" if encoded[-1] != "A" else "B") + "." + sig
    with pytest.raises(CredentialTokenError):
        verify_credential_token(SECRET, tampered)


def test_tampered_signature_fails():
    """Modifying the signature bytes is detected by HMAC verification."""
    token = issue_credential_token(
        SECRET, provider_id="anthropic", account_id="default", ttl_seconds=60
    )
    encoded, _, sig = token.rpartition(".")
    flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    with pytest.raises(CredentialTokenError):
        verify_credential_token(SECRET, f"{encoded}.{flipped}")


def test_expired_token_fails():
    """Tokens past their ``exp`` are rejected."""
    token = issue_credential_token(
        SECRET,
        provider_id="anthropic",
        account_id="default",
        ttl_seconds=60,
        now=_fixed_now(),
    )
    with pytest.raises(CredentialTokenError) as excinfo:
        verify_credential_token(SECRET, token, now=_fixed_now() + 120)
    assert "expired" in str(excinfo.value).lower()


def test_token_at_exact_exp_is_expired():
    """``exp`` is treated as the boundary: at-or-after exp the token fails.

    The half-open interval ``[issued, exp)`` is valid; ``exp`` is not.
    This avoids the off-by-one risk where ``exp == now`` would otherwise
    depend on the caller's clock-resolution rounding.
    """
    token = issue_credential_token(
        SECRET,
        provider_id="anthropic",
        account_id="default",
        ttl_seconds=60,
        now=_fixed_now(),
    )
    with pytest.raises(CredentialTokenError):
        verify_credential_token(SECRET, token, now=_fixed_now() + 60)


def test_malformed_tokens_rejected():
    """Various malformed tokens raise a clear error."""
    # Empty
    with pytest.raises(CredentialTokenError):
        verify_credential_token(SECRET, "")
    # Missing separator
    with pytest.raises(CredentialTokenError):
        verify_credential_token(SECRET, "nodot")
    # Empty payload
    with pytest.raises(CredentialTokenError):
        verify_credential_token(SECRET, "." + "a" * 64)
    # Garbage payload (not valid base64)
    with pytest.raises(CredentialTokenError):
        verify_credential_token(SECRET, "!!!.ffff")
    # Garbage payload (valid base64 but not JSON)
    import base64

    valid_b64 = base64.urlsafe_b64encode(b"not json").rstrip(b"=").decode()
    with pytest.raises(CredentialTokenError):
        verify_credential_token(SECRET, f"{valid_b64}.{sig64_for(b'not json', SECRET)}")


def sig64_for(payload_bytes: bytes, secret: str) -> str:
    import hashlib
    import hmac

    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def test_version_mismatch_rejected():
    """A token carrying an unknown version is refused (forces re-issue)."""
    # Build a token by hand with version=99
    import base64
    import hashlib
    import hmac
    import json

    payload = json.dumps(
        {"v": 99, "pid": "anthropic", "aid": "default", "exp": int(_fixed_now()) + 3600},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    sig = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    token = f"{base64.urlsafe_b64encode(payload).rstrip(b'=').decode()}.{sig}"
    with pytest.raises(CredentialTokenError) as excinfo:
        verify_credential_token(SECRET, token, now=_fixed_now())
    assert "version" in str(excinfo.value).lower()


def test_issue_rejects_non_positive_ttl():
    """TTL of 0 or negative would produce already-expired tokens."""
    with pytest.raises(CredentialTokenError):
        issue_credential_token(SECRET, provider_id="anthropic", account_id="default", ttl_seconds=0)
    with pytest.raises(CredentialTokenError):
        issue_credential_token(
            SECRET, provider_id="anthropic", account_id="default", ttl_seconds=-1
        )


def test_issue_rejects_empty_inputs():
    """Empty provider_id, account_id, or secret is rejected at issue time."""
    with pytest.raises(CredentialTokenError):
        issue_credential_token(SECRET, provider_id="", account_id="default", ttl_seconds=60)
    with pytest.raises(CredentialTokenError):
        issue_credential_token(SECRET, provider_id="anthropic", account_id="", ttl_seconds=60)
    with pytest.raises(CredentialTokenError):
        issue_credential_token("", provider_id="anthropic", account_id="default", ttl_seconds=60)


def test_claims_is_expired_helper():
    """``is_expired`` mirrors the verification behaviour."""
    claims = CredentialTokenClaims(
        provider_id="x", account_id="y", exp=int(_fixed_now()) + 10, version=1
    )
    assert not claims.is_expired(now=_fixed_now())
    # Just before expiry: not expired.
    assert not claims.is_expired(now=_fixed_now() + 9.5)
    # At-or-after expiry: expired.
    assert claims.is_expired(now=_fixed_now() + 10)


def test_verify_rejects_non_ascii_token():
    """Non-ASCII characters in the token string trip
    ``hmac.compare_digest``'s ``TypeError`` — we must catch them up front
    and raise ``CredentialTokenError`` (which callers map to 401) rather
    than letting them propagate as 500s."""
    # Encode a payload + signature using UTF-8 bytes that straddle non-ASCII
    # code points. The encoded payload can include any base64url byte
    # (ASCII safe), so we synthesise an obviously non-ASCII signature.
    with pytest.raises(CredentialTokenError) as excinfo:
        verify_credential_token(SECRET, "abc.café", now=_fixed_now())
    assert "non-ascii" in str(excinfo.value).lower()


def test_verify_rejects_non_ascii_secret():
    """The HMAC secret must also be ASCII (it's a server-side constant, but
    tests loading it from an arbitrary source should fail loudly, not 500
    at verification time)."""
    token = issue_credential_token(
        SECRET, provider_id="anthropic", account_id="default", ttl_seconds=60, now=_fixed_now()
    )
    with pytest.raises(CredentialTokenError) as excinfo:
        verify_credential_token("naïve-secret", token, now=_fixed_now())
    assert "non-ascii" in str(excinfo.value).lower()
