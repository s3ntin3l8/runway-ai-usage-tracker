"""Proactive OAuth token refresh for supported providers."""

import json
import logging
import time

import httpx

from app.core.config import settings
from app.core.utils import IdentityExtractor, safe_write_json, scrub_log

logger = logging.getLogger(__name__)

_REFRESH_ENDPOINTS: dict[str, str] = {
    "anthropic": "https://platform.claude.com/v1/oauth/token",
    "gemini": "https://oauth2.googleapis.com/token",
    "chatgpt": "https://auth.openai.com/oauth/token",
}

_PROVIDER_CLIENT_IDS: dict[str, str] = {
    "anthropic": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    "gemini": "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com",
    "chatgpt": "app_EMoamEEZ73f0CkXaXp7hrann",
}

# Gemini CLI's OAuth client is a Google "desktop app" client — Google requires
# `client_secret` for the refresh_token grant on these too, and the CLI ships
# it embedded in its binary (public by necessity). Sourced from
# github.com/google-gemini/gemini-cli packages/core/src/code_assist/oauth2.ts.
_GEMINI_CLI_CLIENT_SECRET = "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"


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
        gem_client_id: str | None = tokens.get("client_id") or settings.GEMINI_OAUTH_CLIENT_ID
        # Gemini CLI tokens carry the client_id as the JWT `aud` claim, then
        # finally the well-known CLI client_id baked into the published binary.
        if not gem_client_id and tokens.get("id_token"):
            gem_client_id = IdentityExtractor.get_client_id_from_jwt(tokens["id_token"])
        if not gem_client_id:
            gem_client_id = _PROVIDER_CLIENT_IDS.get("gemini") or None
        if gem_client_id:
            payload["client_id"] = gem_client_id
        client_secret = (
            tokens.get("client_secret")
            or settings.GEMINI_OAUTH_CLIENT_SECRET
            or _GEMINI_CLI_CLIENT_SECRET
        )
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
    # Google returns a fresh id_token when the scope includes openid — we have
    # to capture it because token_health uses its `exp` claim to classify the
    # entry's status. Keeping the old one would leave the row stuck as expired.
    if "id_token" in data:
        updated["id_token"] = data["id_token"]
    # Record the new access-token expiry (ms epoch, gemini-cli/Google format).
    # Opaque access tokens carry no JWT `exp`, so without this the refreshed entry
    # would report a stale expiry and a staler sidecar push could clobber it.
    expires_in = data.get("expires_in")
    if expires_in is not None:
        try:
            updated["expiry_date"] = str(int(time.time() * 1000) + int(float(expires_in) * 1000))
        except (TypeError, ValueError):
            pass

    logger.info(f"Refreshed OAuth token for provider={scrub_log(provider)}")
    return updated


def persist_to_local_file(provider: str, new_tokens: dict[str, str], source: str | None) -> None:
    """Write refreshed tokens back to the on-disk credentials file when the
    cached entry was sourced locally (source='server').

    Without this, the local collector's next `_get_current_token` reads the
    stale file and clobbers the just-refreshed cache entry — which is exactly
    why the Token Health row would flicker back to "expired" after a successful
    manual refresh or auto-refresh.
    """
    if source != "server":
        return
    if provider != "gemini":
        # Anthropic's local file format is more involved — not handled yet.
        return

    path = settings.GEMINI_OAUTH_PATH
    try:
        with open(path) as f:
            creds = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        logger.warning(f"Skipping local gemini persist (read failed): {e}")
        return

    if "oauth_token" in new_tokens:
        creds["access_token"] = new_tokens["oauth_token"]
    if "refresh_token" in new_tokens:
        creds["refresh_token"] = new_tokens["refresh_token"]
    if "id_token" in new_tokens:
        creds["id_token"] = new_tokens["id_token"]
        # The id_token's exp matches the access_token's lifetime for Google,
        # so we can derive the gcloud-style expiry_date (milliseconds) from it.
        payload = IdentityExtractor.extract_jwt_payload(new_tokens["id_token"])
        exp = payload.get("exp")
        if exp is not None:
            try:
                creds["expiry_date"] = int(float(exp) * 1000)
            except (TypeError, ValueError):
                logger.debug("Could not parse JWT exp claim as number")

    try:
        safe_write_json(path, creds)
        logger.debug(f"Persisted refreshed gemini credentials to {path}")
    except Exception as e:
        logger.warning(f"Could not persist gemini credentials to {path}: {e}")
