"""Proactive OAuth token refresh for supported providers."""

import json
import logging
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.log_redaction import redact_secrets
from app.core.utils import IdentityExtractor, scrub_log
from app.services.refresh_policy import ROTATING_REFRESH_PROVIDERS, machine_owns_credential

__all__ = [
    "ROTATING_REFRESH_PROVIDERS",
    "machine_owns_credential",
    "refresh_oauth_token",
]

logger = logging.getLogger(__name__)


_REFRESH_ENDPOINTS: dict[str, str] = {
    "anthropic": "https://platform.claude.com/v1/oauth/token",
    "gemini": "https://oauth2.googleapis.com/token",
    "chatgpt": "https://auth.openai.com/oauth/token",
    "xai": "https://auth.x.ai/oauth2/token",
}

_PROVIDER_CLIENT_IDS: dict[str, str] = {
    "anthropic": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    "gemini": "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com",
    "chatgpt": "app_EMoamEEZ73f0CkXaXp7hrann",
    "xai": "b1a00492-073a-47ea-816f-4c329264a828",  # pragma: allowlist secret
}

# Gemini CLI's OAuth client is a Google "desktop app" client — Google requires
# `client_secret` for the refresh_token grant on these too, and the CLI ships
# it embedded in its binary (public by necessity). Sourced from
# github.com/google-gemini/gemini-cli packages/core/src/code_assist/oauth2.ts.
_GEMINI_CLI_CLIENT_SECRET = "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"


# Anthropic refresh shape (issue #577): a JSON body and no header but Content-Type plus a neutral
# User-Agent. Claude Code sends no User-Agent / anthropic-beta of its own here, and the form body
# with those headers that this module used to send is answered with HTTP 429.
ANTHROPIC_REFRESH_USER_AGENT = "runway-ai-usage-tracker"


def _anthropic_refresh_request(
    refresh_token: str, tokens: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    """``(json_body, headers)`` for an Anthropic refresh, shaped exactly like Claude Code's own.

    Verified against the live endpoint (issue #576/#577): a **JSON** body with ``Content-Type``
    as the only header besides a neutral User-Agent is accepted; the form-encoded body plus
    ``User-Agent: claude-code/2.1.69`` and ``anthropic-beta`` that this module used to send was
    answered with HTTP 429 every time. ``scope`` is *not* what the endpoint cares about: it is
    sent only when the login's own scope is known (Claude Code sends the scopes it was granted),
    and omitted otherwise — the endpoint then grants the login's default set, whereas an
    explicit list can be rejected for a login that was granted fewer. Refresh tokens are strictly
    single-use, so a rejected or lost response cannot be retried.

    *tokens* is a bundle's token map; ``scope`` may be stored as anything (a list, an int, None),
    so only a non-blank string is used.
    """
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": tokens.get("client_id") or settings.CLAUDE_OAUTH_CLIENT_ID,
    }
    scope = tokens.get("scope")
    if isinstance(scope, str) and scope.split():
        body["scope"] = " ".join(scope.split())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": ANTHROPIC_REFRESH_USER_AGENT,
    }
    return body, headers


async def refresh_oauth_token(provider: str, tokens: dict[str, str]) -> dict[str, str]:
    """
    Attempt to exchange a refresh_token for new access credentials.

    Returns an updated copy of *tokens* with the new access_token (and
    refresh_token if the provider rotates it).

    Raises:
        ValueError: provider has no known refresh endpoint or no refresh token found.
        httpx.HTTPStatusError: upstream returned a non-2xx response. The
            response body (redacted) is logged at WARNING before re-raising,
            because the OAuth error code it carries is the whole diagnosis.
    """
    endpoint = _REFRESH_ENDPOINTS.get(provider)
    if not endpoint:
        raise ValueError(f"No refresh endpoint known for provider: {provider}")

    # The provider supports token refresh, but this specific credential set
    # lacks a refresh token (e.g. static API key or session cookie).
    refresh_val = tokens.get("refresh_token") or tokens.get("xai_refresh")
    if not refresh_val:
        raise ValueError(f"No refresh_token found in tokens for provider: {provider}")

    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_val,
    }

    # Provider-specific extra params
    json_body: dict[str, str] | None = None
    if provider == "anthropic":
        json_body, anthropic_headers = _anthropic_refresh_request(refresh_val, tokens)
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
    elif provider == "xai":
        payload["client_id"] = tokens.get("client_id") or _PROVIDER_CLIENT_IDS.get("xai", "")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if provider == "xai":
        headers["User-Agent"] = "opencode/1.0"

    async with httpx.AsyncClient(timeout=15.0) as client:
        if json_body is not None:
            resp = await client.post(endpoint, json=json_body, headers=anthropic_headers)
        else:
            resp = await client.post(endpoint, data=payload, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            # The status line alone drops the only part that explains a dead
            # refresh lineage: `invalid_grant` (rotated or revoked grant) vs
            # `invalid_request` (malformed) vs `invalid_client`. Keep the body
            # — redacted, single-lined, truncated — then re-raise so every
            # caller sees exactly the exception it saw before (issue #474).
            logger.warning(
                "Token refresh failed for provider=%s status=%s body=%s",
                scrub_log(provider),
                resp.status_code,
                _describe_failure_body(str(resp.text)),
            )
            raise
        data = resp.json()

    updated = dict(tokens)
    if "access_token" in data:
        if provider == "xai":
            updated["xai_access"] = data["access_token"]
        else:
            updated["oauth_token"] = data["access_token"]
    if "refresh_token" in data:
        if provider == "xai":
            updated["xai_refresh"] = data["refresh_token"]
        else:
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
            pass  # non-numeric expires_in — leave expiry_date unchanged

    logger.info(f"Refreshed OAuth token for provider={scrub_log(provider)}")
    return updated


def _describe_failure_body(raw: str) -> str:
    """Redacted, single-line, at-most-300-char rendering of a failure body.

    Parsed first: on a JSON payload the dict path of `redact_secrets` replaces
    whole values under credential-shaped keys, which redacts more thoroughly
    than the string-shape regexes and avoids their cosmetic leftovers (a
    base64 ``=`` left behind after a JWT match). Bodies that don't parse —
    HTML error pages, truncated payloads — fall back to string redaction.
    """
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        parsed = raw[:4000]
    redacted = redact_secrets(parsed)
    if not isinstance(redacted, str):
        redacted = json.dumps(redacted, ensure_ascii=False)
    return scrub_log(redacted)[:300]
