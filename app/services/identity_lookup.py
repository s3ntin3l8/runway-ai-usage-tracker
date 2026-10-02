"""Ask an upstream provider who a credential belongs to.

Used when a sidecar found a credential it could not identify locally and sent it to the
server to be verified (``identity_pending``). The caller sets the collector's
``account_id`` from the answer, and ``CollectorManager`` then binds that identity to the
exact source it came from.
"""

import logging

import httpx

from app.services.account_identity import EMAIL_RE

logger = logging.getLogger(__name__)

GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


async def google_userinfo_email(client: httpx.AsyncClient, access_token: str) -> str | None:
    """The Google account email behind *access_token*, or ``None`` if it can't be resolved.

    Needs the ``userinfo.email`` scope, which the Gemini CLI and Antigravity (agy) OAuth
    clients both request. Never raises: an unresolved identity just stays pending.
    """
    try:
        resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            email = resp.json().get("email")
            return email if isinstance(email, str) and "@" in email else None
    except Exception:
        logger.debug("Could not resolve a Google account email from userinfo", exc_info=True)
    return None


async def claude_profile_email(client: httpx.AsyncClient, headers: dict[str, str]) -> str | None:
    """The Claude account email of the token's *holder*, or ``None`` if it can't be resolved.

    *headers* carry the bearer (and the OAuth beta header) of the request that just
    succeeded. ``/api/oauth/profile`` answers per user (``account.email``), unlike
    ``/v1/organizations/me``, whose contact can be an org admin. It needs the
    ``user:profile`` scope, so a token without it gets a refusal and stays unresolved.
    Never raises.
    """
    try:
        resp = await client.get(CLAUDE_PROFILE_URL, headers=headers, timeout=5)
        if resp.status_code == 200:
            account = resp.json().get("account")
            email = account.get("email") if isinstance(account, dict) else None
            return email if isinstance(email, str) and EMAIL_RE.match(email) else None
    except Exception:
        logger.debug("Could not resolve a Claude account email from the profile", exc_info=True)
    return None
