"""Ask an upstream provider who a credential belongs to.

Used when a sidecar found a credential it could not identify locally and sent it to the
server to be verified (``identity_pending``). The caller sets the collector's
``account_id`` from the answer, and ``CollectorManager`` then binds that identity to the
exact source it came from.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


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
