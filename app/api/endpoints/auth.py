"""Built-in session auth: exchange the admin key for an HttpOnly cookie.

Hardens the admin-key path (issue #92) so the secret never lives in the
browser's localStorage. `POST /session` validates the key once and mints a
short-lived signed cookie; `POST /logout` clears it; `POST /revoke-all`
rotates the signing secret to kill every outstanding session (issue #100).
The `X-Admin-Key` header path stays valid for API/script clients.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlmodel import Session

from app.api.endpoints.github_oauth import router as github_router
from app.core.config import settings
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import SESSION_COOKIE, require_admin_key
from app.core.sessions import issue_session, rotate_secret, session_max_age
from app.services import audit_log

router = APIRouter()

# Register auth providers
router.include_router(github_router, prefix="/github", tags=["auth", "github"])


class _SessionLoginRequest(BaseModel):
    key: str
    remember: bool = False


def _set_session_cookie(response: Response, token: str, remember: bool) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=session_max_age(remember),
        httponly=True,
        samesite="strict",
        # Only mark Secure when TLS is terminated upstream — otherwise the
        # cookie would never be sent over plain-http localhost dev.
        secure=settings.TLS_TERMINATED,
        path="/",
    )


@router.post("/session")
@limiter.limit("10/minute")
async def create_session(
    request: Request, response: Response, body: _SessionLoginRequest
) -> dict[str, bool]:
    """Validate the admin key and set an HttpOnly session cookie.

    Rate-limited to throttle key guessing (the header path has no such
    throttle today). When no admin key is configured the instance is open,
    so there's nothing to log into — report authenticated without a cookie.
    """
    if settings.ADMIN_API_KEY is None:
        return {"is_authenticated": True}
    if not hmac.compare_digest(body.key, settings.ADMIN_API_KEY):
        raise HTTPException(status_code=403, detail="Invalid admin key")
    _set_session_cookie(response, issue_session(body.remember), body.remember)
    return {"is_authenticated": True}


@router.post("/logout", status_code=204)
async def logout() -> Response:
    """Clear this browser's session cookie. Other sessions are unaffected."""
    # Set the deletion on the response we actually return — a separate injected
    # Response would be discarded along with its Set-Cookie header.
    response = Response(status_code=204)
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="strict",
        secure=settings.TLS_TERMINATED,
    )
    return response


@router.post("/revoke-all", status_code=204)
@limiter.limit("6/minute")
async def revoke_all_sessions(
    request: Request,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> Response:
    """Rotate SESSION_SECRET — invalidates every session everywhere (#100).

    Cheap "log out everywhere": no provider secrets are re-encrypted since
    the session key is independent of DB_ENCRYPTION_KEY.
    """
    rotate_secret()
    audit_log.record(session, request, action="auth.revoke_all", target_id=None)
    return Response(status_code=204)
