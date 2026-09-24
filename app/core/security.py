"""Admin auth for mutation endpoints.

`resolve_auth` is the single source of truth for "is this caller allowed?"
— shared by the `require_admin_key` dependency and the `/system/settings`
auth probe so the two can never drift. It returns a structured `AuthResult`
(issue #103) carrying both a coarse `actor_type` and, when a reverse proxy
supplies it, the asserted user identity for the audit log.

The companion ``validate_ingest_auth`` is the single source of truth for
the sidecar-facing HMAC scheme used by ``/fleet/ingest`` and (post #288)
``/fleet/credentials/manifest``. Extracted so any future endpoint that
needs the same shape inherits one canonical scheme — a change in the
skew window, header layout, or signature construction can't accidentally
diverge between the two endpoints.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass

from fastapi import Cookie, Header, HTTPException, Request

from app.core.config import settings
from app.core.sessions import verify_session
from app.core.utils import scrub_log

logger = logging.getLogger(__name__)

# Name of the HttpOnly cookie minted by POST /auth/session.
SESSION_COOKIE = "runway_session"

# Static fallback checked when the configured FORWARD_AUTH_USER_HEADER isn't
# present, kept for back-compat with the CGI-style Remote-User convention.
# The primary header name is configurable so Authentik's outpost headers
# (X-authentik-username / -email / -groups) work with no proxy-side renaming.
_REMOTE_USER_FALLBACK_HEADER = "Remote-User"

# Some proxies (Authentik) delimit multi-valued group headers with "|"
# rather than ",", since group names may themselves contain commas.
_GROUP_SPLIT_RE = re.compile(r"[,|\s]+")

# Legacy `actor` strings kept stable for audit-log readers; actor_types that
# aren't listed here label themselves ("localhost", "session", "api-key").
_ACTOR_LABELS = {"none": "no-admin-key-configured"}


@dataclass(frozen=True)
class AuthResult:
    """Outcome of an auth check.

    actor_type ∈ {"localhost","proxy","session","api-key","none"}. actor_id
    is the proxy-asserted user when known. actor_meta carries proxy extras
    (email, groups) destined for the audit log.
    """

    authenticated: bool
    actor_type: str
    actor_id: str | None = None
    actor_meta: dict[str, str] | None = None

    @property
    def actor(self) -> str:
        """Human-readable label for audit_log's legacy `actor` column."""
        if self.actor_id:
            return self.actor_id
        return _ACTOR_LABELS.get(self.actor_type, self.actor_type)


def _proxy_meta(request: Request) -> dict[str, str] | None:
    """Best-effort identity extras a forward-auth proxy may attach."""
    meta: dict[str, str] = {}
    email = request.headers.get(settings.FORWARD_AUTH_EMAIL_HEADER)
    groups = request.headers.get(settings.FORWARD_AUTH_GROUPS_HEADER)
    if email:
        meta["email"] = email
    if groups:
        meta["groups"] = groups
    return meta or None


def _proxy_authorized(request: Request, proxy_user: str) -> bool:
    """Optional defense-in-depth allowlist on top of the IP trust gate.

    Empty allow-lists (the default) trust any user the proxy asserts — the
    IP allowlist plus the identity provider's own app binding is the only
    gate, matching pre-existing behavior. When either list is configured,
    the asserted user must appear in ALLOWED_USERS or share a group (from
    FORWARD_AUTH_GROUPS_HEADER) with ALLOWED_GROUPS.
    """
    allowed_users = settings.forward_auth_allowed_users
    allowed_groups = settings.forward_auth_allowed_groups
    if not allowed_users and not allowed_groups:
        return True
    if proxy_user in allowed_users:
        return True
    if allowed_groups:
        raw = request.headers.get(settings.FORWARD_AUTH_GROUPS_HEADER, "")
        asserted = {g for g in _GROUP_SPLIT_RE.split(raw) if g}
        if asserted & allowed_groups:
            return True
    return False


def resolve_auth(
    request: Request,
    *,
    x_admin_key: str | None,
    session_cookie: str | None,
) -> AuthResult:
    """Evaluate the bypass ladder for a request. Pure aside from `verify_session`.

    Order: no-key-configured → localhost trust → reverse-proxy trust →
    session cookie → X-Admin-Key header.
    """
    # Falsy (None, or a blank string that slipped past config normalization)
    # means no admin key is configured — never treat "" as a comparable key.
    if not settings.ADMIN_API_KEY:
        return AuthResult(True, "none")

    client_host = request.client.host if request.client else None

    # 1. Local trust (zero-touch local usage). Only when client IS localhost
    # AND the server is bound to localhost-only.
    if client_host in ("127.0.0.1", "::1") and settings.APP_HOST in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        return AuthResult(True, "localhost")

    # 2. Reverse-proxy trust — gated by an IP allowlist so the user headers
    # can't be forged by an arbitrary client, with an optional group/user
    # allowlist layered on top. Header names are configurable (defaults
    # match the previous X-Forwarded-User/Remote-User hardcoding) so an
    # Authentik outpost's native X-authentik-* headers work directly.
    proxy_user = request.headers.get(settings.FORWARD_AUTH_USER_HEADER) or request.headers.get(
        _REMOTE_USER_FALLBACK_HEADER
    )
    trusted = settings.trusted_proxy_ips
    if trusted and client_host in trusted and proxy_user and _proxy_authorized(request, proxy_user):
        return AuthResult(True, "proxy", actor_id=proxy_user, actor_meta=_proxy_meta(request))

    # 3. Browser session cookie (admin key already exchanged at /auth/session).
    if verify_session(session_cookie):
        return AuthResult(True, "session")

    # 4. Standard API key — constant-time compare to prevent a timing oracle.
    if x_admin_key is not None and hmac.compare_digest(x_admin_key, settings.ADMIN_API_KEY):
        return AuthResult(True, "api-key")

    return AuthResult(False, "none")


async def require_admin_key(
    request: Request,
    x_admin_key: str = Header(default=None),
    session_cookie: str = Cookie(default=None, alias=SESSION_COOKIE),
) -> None:
    """Gate a mutation on `resolve_auth`. 403 when the caller isn't authenticated.

    Proxy identity headers are read directly off `request.headers` inside
    `resolve_auth` (their names are configurable), not as FastAPI `Header`
    params here — that keeps this signature stable regardless of which
    header names an operator points at.

    Side effect: stashes the full `AuthResult` on `request.state.auth` (and a
    legacy `request.state.admin_actor` string) so the audit log can attribute
    the mutation. See `app.services.audit_log`.
    """
    result = resolve_auth(
        request,
        x_admin_key=x_admin_key,
        session_cookie=session_cookie,
    )
    request.state.auth = result
    request.state.admin_actor = result.actor
    if not result.authenticated:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")


async def validate_ingest_auth(
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
) -> bytes:
    """Shared HMAC pre-flight for ingest-side endpoints (``/fleet/ingest``,
    ``/fleet/credentials/manifest``).

    Returns the raw request body on success so the caller can parse it as
    the next step. Raises ``HTTPException`` with the documented status
    code on any failure — no caller ever needs to know the skew window,
    the body cap, or the signature construction. This is the
    canonical scheme — any change to the HMAC pre-flight MUST land
    here, never in a duplicate copy at the endpoint.

    Errors:
    - 503 ``INGEST_API_KEY`` empty / insecure default (endpoint disabled)
    - 401 missing headers, malformed timestamp, signature mismatch
    - 400 timestamp skew outside ``[now - 300, now + 60]``
    - 413 body larger than 8 MB
    """
    # Read settings via the canonical module path so test fixtures that
    # ``patch("app.core.config.settings")`` (or ``app.api.endpoints.fleet.settings``)
    # observe the same value the helper sees. Importing at call time also
    # avoids a module-load race with conftest fixtures that swap settings
    # before the app is constructed.
    from app.core.config import settings as _settings

    if not _settings.INGEST_API_KEY:
        logger.error("INGEST_API_KEY is empty — sidecar endpoint is disabled")
        raise HTTPException(
            status_code=503,
            detail="Sidecar endpoint not configured: INGEST_API_KEY is empty",
        )
    if _settings.INGEST_API_KEY_IS_INSECURE_DEFAULT:
        logger.error("INGEST_API_KEY is the default insecure value — sidecar endpoint is disabled")
        raise HTTPException(
            status_code=503,
            detail=("Sidecar endpoint not configured: INGEST_API_KEY must be changed from default"),
        )

    if not x_signature or not x_timestamp:
        logger.warning("Sidecar attempt with missing HMAC headers")
        raise HTTPException(
            status_code=401,
            detail="Missing HMAC signature or timestamp",
        )

    try:
        ts = float(x_timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid X-Timestamp format") from None

    now = time.time()
    skew = now - ts
    # 5-minute window for past timestamps, 60s for future drift.
    # ``skew`` is a float we computed — safe to interpolate; the
    # response body in ``detail`` echoes the same value but is
    # serialized via FastAPI's JSON encoder (CR/LF escaping is handled
    # there). The HMAC-mismatch log line below DOES wrap user input,
    # because ``x_signature`` is fully attacker-controlled and is
    # reached before signature validation passes (PR #290 round-2
    # review, Hermes warning #4 + CodeQL log-injection).
    if skew < -60 or skew > 300:
        logger.warning(f"Sidecar attempt with rejected timestamp: {skew:.0f}s difference")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "timestamp_expired" if skew > 0 else "timestamp_future",
                "skew_seconds": round(skew, 1),
                "message": "Clock skew detected. Please check NTP sync on the sidecar machine.",
            },
        )

    body_bytes = await request.body()
    # 8 MB cap. Manifests stay small (one origin per host-provider), ingest
    # bodies batch up to 1000 events (~1 MB worst case), so 8 MB is generous.
    if len(body_bytes) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Request body too large")

    expected_sig = hmac.new(
        _settings.INGEST_API_KEY.encode(),
        f"{x_timestamp}".encode() + body_bytes,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(x_signature, expected_sig):
        # ``scrub_log`` strips CR/LF from ``x_signature`` so a malicious
        # client can't inject extra log lines ("FAKE root login") via a
        # crafted ``X-Signature`` header. The other log lines in this
        # function either use computed floats (skew, body length) or
        # static strings — none reach user input.
        logger.warning(
            f"HMAC mismatch. Received: {scrub_log(x_signature[:8])}... (len: {len(x_signature)})"
        )
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    return body_bytes


def verify_config_signature(request: Request) -> bool:
    """Optional HMAC check for ``GET /api/v1/fleet/config``.

    Returns ``False`` when the request is unsigned (or no ingest key is
    configured, so nothing can be verified) — the caller then serves the
    redacted view. Raises 401 / 400 for a signature that is *present* but
    wrong or stale, so a misconfigured sidecar fails loudly instead of
    silently losing its hints.

    Signed message: ``timestamp + "GET:" + raw query string`` — mirrors
    ``scripts/sidecar_pkg/credentials.py:config_request_signature``. Binding
    the query (``sidecar_id=…``) stops a captured signature from being
    replayed to read another machine's scoped hints.
    """
    from app.core.config import settings as _settings

    x_signature = request.headers.get("X-Signature")
    x_timestamp = request.headers.get("X-Timestamp")
    if not x_signature or not x_timestamp:
        return False
    if not _settings.INGEST_API_KEY or _settings.INGEST_API_KEY_IS_INSECURE_DEFAULT:
        return False
    try:
        skew = time.time() - float(x_timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid X-Timestamp format") from None
    if skew < -60 or skew > 300:
        raise HTTPException(status_code=400, detail="X-Timestamp outside the allowed window")
    expected = hmac.new(
        _settings.INGEST_API_KEY.encode(),
        f"{x_timestamp}GET:{request.url.query}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(x_signature, expected):
        source = request.client.host if request.client else "unknown"
        logger.warning("fleet/config: HMAC mismatch from %s", scrub_log(source))
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")
    # Single use: a captured signed GET (on-path, within the timestamp
    # window) must not be replayable for the hints + credential tokens.
    now = time.time()
    for seen_sig, seen_at in list(_SEEN_CONFIG_SIGNATURES.items()):
        if now - seen_at > _CONFIG_SIGNATURE_TTL:
            del _SEEN_CONFIG_SIGNATURES[seen_sig]
    if x_signature in _SEEN_CONFIG_SIGNATURES:
        raise HTTPException(status_code=401, detail="Replayed signature")
    _SEEN_CONFIG_SIGNATURES[x_signature] = now
    return True


# Signatures already accepted by verify_config_signature, kept for the whole
# accepted timestamp window (+60s future skew) so each is usable once.
_CONFIG_SIGNATURE_TTL = 360
_SEEN_CONFIG_SIGNATURES: dict[str, float] = {}


def is_loopback_bind() -> bool:
    """True when the server only listens on loopback (local topology)."""
    from app.core.config import settings as _settings

    return _settings.APP_HOST in ("127.0.0.1", "localhost", "::1")
