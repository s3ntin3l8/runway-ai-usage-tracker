import asyncio
import json
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.utils import IdentityExtractor, safe_write_json
from app.services.collector_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()


class DeviceFlowInitResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DeviceFlowPollRequest(BaseModel):
    device_code: str


class DeviceFlowStatusResponse(BaseModel):
    authenticated: bool
    account: str | None = None
    name: str | None = None
    email: str | None = None


@router.get("/init", response_model=DeviceFlowInitResponse)
@limiter.limit("5/minute")
async def init_device_flow(request: Request) -> DeviceFlowInitResponse:
    """Step 1: Get the device code and user code from GitHub."""
    async with httpx.AsyncClient() as client:
        try:
            # GitHub Device Flow STEP 1: Request codes
            # Using data= (form-encoded) as it's often more reliable for this endpoint
            logger.debug(
                f"Initiating GitHub Device Flow for client_id: {settings.GITHUB_CLIENT_ID}"
            )
            resp = await client.post(
                "https://github.com/login/device/code",
                data={
                    "client_id": settings.GITHUB_CLIENT_ID,
                    "scope": "read:user user:email",
                },
                headers={
                    "Accept": "application/json",
                    "User-Agent": "GitHubCopilotChat/0.26.7",
                },
                timeout=15.0,
            )

            if resp.status_code != 200:
                logger.error(f"GitHub Device Flow init failed: {resp.status_code} {resp.text}")
                # Provide a more descriptive error if we can
                try:
                    error_data = resp.json()
                    detail = error_data.get(
                        "error_description",
                        f"GitHub error: {error_data.get('error', resp.status_code)}",
                    )
                    raise HTTPException(status_code=resp.status_code, detail=detail)
                except (json.JSONDecodeError, KeyError):
                    raise HTTPException(
                        status_code=resp.status_code, detail=f"GitHub API error: {resp.status_code}"
                    )

            data = resp.json()
            return DeviceFlowInitResponse(**data)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to initiate GitHub Device Flow: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to initiate login with GitHub: {str(e)}"
            )


@router.post("/poll")
@limiter.limit("5/minute")
async def poll_device_flow(request: Request, body: DeviceFlowPollRequest) -> dict[str, Any]:
    """Step 2: Poll for the access token."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": settings.GITHUB_CLIENT_ID,
                    "device_code": body.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={
                    "Accept": "application/json",
                    "User-Agent": "GitHubCopilotChat/0.26.7",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"GitHub polling response: {data}")

            if "error" in data:
                error = data["error"]
                if error == "authorization_pending":
                    return {"status": "pending"}
                if error == "slow_down":
                    return {"status": "slow_down", "interval": data.get("interval", 5)}
                raise HTTPException(status_code=400, detail=data.get("error_description", error))

            if "access_token" in data:
                # Get user info for name/email right away
                user_info = {}
                try:
                    # 1. Fetch basic user info (login, name)
                    user_resp = await client.get(
                        "https://api.github.com/user",
                        headers={"Authorization": f"Bearer {data['access_token']}"},
                        timeout=10.0,
                    )
                    if user_resp.status_code == 200:
                        user_data = user_resp.json()
                        user_info["login"] = user_data.get("login")
                        user_info["name"] = user_data.get("name")
                        user_info["email"] = user_data.get("email")

                    # 2. Fetch all emails to find the "real" one (not the noreply one)
                    email_resp = await client.get(
                        "https://api.github.com/user/emails",
                        headers={"Authorization": f"Bearer {data['access_token']}"},
                        timeout=10.0,
                    )
                    if email_resp.status_code == 200:
                        emails = email_resp.json()
                        user_info["email"] = IdentityExtractor.extract_best_email(emails)
                except Exception as e:
                    logger.warning(f"Failed to fetch user info during OAuth poll: {e}")

                # Save the token + user info
                await save_token({**data, **user_info})
                return {"status": "success"}

            raise HTTPException(status_code=500, detail="Unexpected response from GitHub")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error polling GitHub Device Flow: {e}")
            raise HTTPException(status_code=500, detail="Error communicating with GitHub")


@router.get("/status", response_model=DeviceFlowStatusResponse)
async def get_status() -> DeviceFlowStatusResponse:
    """Check if GitHub is authenticated."""
    if os.path.exists(settings.GITHUB_OAUTH_PATH):
        try:
            with open(settings.GITHUB_OAUTH_PATH) as f:
                creds = json.load(f)

            async with httpx.AsyncClient() as client:
                token = creds.get("access_token")
                if not token:
                    return DeviceFlowStatusResponse(authenticated=False)

                # Fetch fresh info to verify token and get current details
                resp = await client.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=5.0,
                )

                if resp.status_code == 200:
                    user_data = resp.json()

                    # Try to get the real email if possible
                    fresh_email = user_data.get("email")
                    try:
                        email_resp = await client.get(
                            "https://api.github.com/user/emails",
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=5.0,
                        )
                        if email_resp.status_code == 200:
                            emails = email_resp.json()
                            fresh_email = IdentityExtractor.extract_best_email(emails)
                    except Exception:
                        pass

                    new_info = {
                        "login": user_data.get("login"),
                        "name": user_data.get("name"),
                        "email": fresh_email,
                    }
                    if any(creds.get(k) != v for k, v in new_info.items() if v is not None):
                        await save_token({**creds, **new_info})

                    return DeviceFlowStatusResponse(
                        authenticated=True,
                        account=user_data.get("login"),
                        name=user_data.get("name"),
                        email=fresh_email,
                    )
                logger.warning(f"GitHub identity check returned {resp.status_code}: {resp.text}")
                # API failed, but we have a token. Fallback to cached info.
                return DeviceFlowStatusResponse(
                    authenticated=True,
                    account=creds.get("login"),
                    name=creds.get("name"),
                    email=creds.get("email"),
                )
        except Exception as e:
            logger.error(f"Error checking GitHub status: {e}")
            # If we failed to read file or something else, but file exists,
            # we might still be authenticated if the token is valid elsewhere
            pass
    return DeviceFlowStatusResponse(authenticated=False)


@router.post("/logout")
async def logout() -> dict[str, str]:
    """Clear the stored GitHub token."""
    if os.path.exists(settings.GITHUB_OAUTH_PATH):
        try:
            await asyncio.to_thread(os.remove, settings.GITHUB_OAUTH_PATH)
            # Trigger immediate registry update to purge cards from dashboard
            await manager.collect_one("github")
            return {"status": "success"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to logout: {e}")
    return {"status": "success"}


async def save_token(data: dict):
    """Securely save the GitHub token to disk."""
    config_dir = os.path.dirname(settings.GITHUB_OAUTH_PATH)
    if not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)

    token_data = {
        "access_token": data["access_token"],
        "token_type": data.get("token_type", "bearer"),
        "scope": data.get("scope", ""),
        "login": data.get("login"),
        "name": data.get("name"),
        "email": data.get("email"),
    }

    def _write():
        safe_write_json(settings.GITHUB_OAUTH_PATH, token_data)

    await asyncio.to_thread(_write)
    logger.info(f"GitHub OAuth token saved to {settings.GITHUB_OAUTH_PATH}")
