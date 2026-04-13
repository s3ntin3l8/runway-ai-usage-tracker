"""Proactive OAuth token refresh for supported providers."""
import httpx
import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Known OAuth token endpoints per provider
_REFRESH_ENDPOINTS: Dict[str, str] = {
    "anthropic": "https://platform.claude.com/v1/oauth/token",
    "gemini": "https://oauth2.googleapis.com/token",
}


async def refresh_oauth_token(provider: str, tokens: Dict[str, str]) -> Dict[str, str]:
    """
    Attempt to exchange a refresh_token for new access credentials.

    Returns an updated copy of *tokens* with the new access_token (and
    refresh_token if the provider rotates it).

    Raises:
        ValueError: provider has no known refresh endpoint.
        httpx.HTTPStatusError: upstream returned a non-2xx response.
    """
    endpoint = _REFRESH_ENDPOINTS.get(provider)
    if not endpoint:
        raise ValueError(f"No refresh endpoint known for provider: {provider}")

    payload: Dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }

    # Provider-specific extra params
    if provider == "anthropic":
        if "client_id" in tokens:
            payload["client_id"] = tokens["client_id"]
    elif provider == "gemini":
        if tokens.get("client_id"):
            payload["client_id"] = tokens["client_id"]
        if tokens.get("client_secret"):
            payload["client_secret"] = tokens["client_secret"]

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    # Anthropic OAuth endpoint requires these headers (matches anthropic_oauth.py)
    if provider == "anthropic":
        headers["User-Agent"] = "claude-code/2.1.69"
        headers["anthropic-beta"] = "oauth-2025-04-20"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            endpoint,
            data=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    updated = dict(tokens)
    if "access_token" in data:
        updated["oauth_token"] = data["access_token"]
    if "refresh_token" in data:
        updated["refresh_token"] = data["refresh_token"]

    logger.info(f"Refreshed OAuth token for provider={provider}")
    return updated
