"""Browser session cookies for the hardened admin-key auth path (issue #92).

The admin key is exchanged once at ``POST /auth/session`` for an
``HttpOnly`` cookie so the secret never lives in ``localStorage`` (where
any XSS can read it). The cookie is a Fernet token carrying only an expiry
— no identity, since the built-in path has a single admin.

The signing key (``SESSION_SECRET``) is generated on first use and stored
encrypted-at-rest in ``system_config``, kept deliberately separate from
``DB_ENCRYPTION_KEY``: rotating it (``POST /auth/revoke-all``) invalidates
every outstanding session at once without re-encrypting provider secrets
(issue #100). It is never derived from ``ADMIN_API_KEY`` — hashing a
secret like that trips CodeQL ``py/weak-sensitive-data-hashing`` and would
couple session lifetime to key rotation.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.core.encryption import encryption_service
from app.models.db import SystemConfig

logger = logging.getLogger(__name__)

# Cached signer keyed on the active SESSION_SECRET. Guarded by a lock so a
# burst of concurrent logins doesn't generate competing secrets on first use.
_signer: Fernet | None = None
_lock = threading.Lock()


def _get_or_create_secret(session: Session) -> str:
    """Return the stored SESSION_SECRET, minting and persisting one if absent.

    The secret is a Fernet key, stored encrypted with DB_ENCRYPTION_KEY.
    """
    cfg = session.exec(select(SystemConfig)).first()
    if cfg is None:
        cfg = SystemConfig()
        session.add(cfg)
    if cfg.session_secret_encrypted:
        return encryption_service.decrypt_string(cfg.session_secret_encrypted)

    secret = Fernet.generate_key().decode()
    cfg.session_secret_encrypted = encryption_service.encrypt_string(secret)
    session.commit()
    logger.info("Generated a new session signing secret.")
    return secret


def _get_signer() -> Fernet:
    """Lazily build and cache the Fernet signer from the stored secret."""
    global _signer
    if _signer is not None:
        return _signer
    with _lock:
        if _signer is None:
            with Session(engine) as session:
                _signer = Fernet(_get_or_create_secret(session).encode())
    return _signer


def session_max_age(remember: bool) -> int:
    """Cookie lifetime in seconds for the default vs. remember-me path."""
    if remember:
        return settings.SESSION_REMEMBER_DAYS * 24 * 3600
    return settings.SESSION_LIFETIME_HOURS * 3600


def issue_session(remember: bool) -> str:
    """Mint a signed session token whose expiry matches the cookie's Max-Age.

    The Fernet token's trailing base64 `=` padding is stripped: `=` forces
    cookie-value quoting, and a quoted value containing `=` fails to survive
    the cookie round-trip. `verify_session` restores the padding.
    """
    exp = datetime.now(UTC).timestamp() + session_max_age(remember)
    payload = json.dumps({"exp": exp}, separators=(",", ":"))
    return _get_signer().encrypt(payload.encode()).decode().rstrip("=")


def verify_session(token: str | None) -> bool:
    """True iff the token is a signature-valid, unexpired session.

    Any failure mode — missing token, tamper, wrong key (e.g. after a
    revoke-all rotation), malformed payload, or past expiry — returns False.
    """
    if not token:
        return False
    # Restore the base64 padding stripped by issue_session.
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = _get_signer().decrypt(padded.encode())
        exp = float(json.loads(raw).get("exp", 0))
    except (InvalidToken, ValueError, json.JSONDecodeError, TypeError):
        return False
    return exp > datetime.now(UTC).timestamp()


def rotate_secret() -> None:
    """Generate a fresh SESSION_SECRET, invalidating every existing session.

    Backs ``POST /auth/revoke-all`` — the cheap "log out everywhere" that
    doesn't touch DB_ENCRYPTION_KEY or provider secrets.
    """
    global _signer
    with _lock:
        secret = Fernet.generate_key().decode()
        with Session(engine) as session:
            cfg = session.exec(select(SystemConfig)).first()
            if cfg is None:
                cfg = SystemConfig()
                session.add(cfg)
            cfg.session_secret_encrypted = encryption_service.encrypt_string(secret)
            session.commit()
        _signer = Fernet(secret.encode())
    logger.info("Rotated session signing secret — all existing sessions revoked.")
