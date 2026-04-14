"""Proactive OAuth token refresh for supported providers."""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_REFRESH_ENDPOINTS: dict[str, str] = {
    "anthropic": "https://platform.claude.com/v1/oauth/token",
    "gemini": "https://oauth2.googleapis.com/token",
    "chatgpt": "https://auth.openai.com/oauth/token",
}

_PROVIDER_CLIENT_IDS: dict[str, str] = {
    "anthropic": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    "gemini": "",
    "chatgpt": "app_EMoamEEZ73f0CkXaXp7hrann",
}


async def refresh_oauth_token(provider: str, tokens: dict[str, str]) -> dict[str, str]:
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

    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }

    # Provider-specific extra params
    if provider == "anthropic":
        client_id = tokens.get("client_id") or settings.CLAUDE_OAUTH_CLIENT_ID
        payload["client_id"] = client_id
    elif provider == "gemini":
        client_id = tokens.get("client_id") or settings.GEMINI_OAUTH_CLIENT_ID
        if client_id:
            payload["client_id"] = client_id
        client_secret = tokens.get("client_secret") or settings.GEMINI_OAUTH_CLIENT_SECRET
        if client_secret:
            payload["client_secret"] = client_secret
    elif provider == "chatgpt":
        payload["client_id"] = _PROVIDER_CLIENT_IDS.get("chatgpt", "")
        payload["scope"] = "openid profile email"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
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
