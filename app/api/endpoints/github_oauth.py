import os
import json
import logging
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.core.config import settings

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
    account: Optional[str] = None

@router.get("/init", response_model=DeviceFlowInitResponse)
async def init_device_flow():
    """Step 1: Get the device code and user code from GitHub."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                "https://github.com/login/device/code",
                data={
                    "client_id": settings.GITHUB_CLIENT_ID,
                    "scope": "repo,read:org,user"
                },
                headers={"Accept": "application/json"}
            )
            resp.raise_for_status()
            data = resp.json()
            return DeviceFlowInitResponse(**data)
        except Exception as e:
            logger.error(f"Failed to initiate GitHub Device Flow: {e}")
            raise HTTPException(status_code=500, detail="Failed to initiate login with GitHub")

@router.post("/poll")
async def poll_device_flow(request: DeviceFlowPollRequest):
    """Step 2: Poll for the access token."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": settings.GITHUB_CLIENT_ID,
                    "device_code": request.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
                },
                headers={"Accept": "application/json"}
            )
            resp.raise_for_status()
            data = resp.json()
            
            if "error" in data:
                error = data["error"]
                if error == "authorization_pending":
                    return {"status": "pending"}
                elif error == "slow_down":
                    return {"status": "slow_down", "interval": data.get("interval", 5)}
                else:
                    raise HTTPException(status_code=400, detail=data.get("error_description", error))
            
            if "access_token" in data:
                # Save the token
                await save_token(data)
                return {"status": "success"}
                
            raise HTTPException(status_code=500, detail="Unexpected response from GitHub")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error polling GitHub Device Flow: {e}")
            raise HTTPException(status_code=500, detail="Error communicating with GitHub")

@router.get("/status", response_model=DeviceFlowStatusResponse)
async def get_status():
    """Check if GitHub is authenticated."""
    if os.path.exists(settings.GITHUB_OAUTH_PATH):
        try:
            async with httpx.AsyncClient() as client:
                # We can't easily check validity without an API call, but we can verify presence
                token = settings.GITHUB_TOKEN
                if not token:
                    return DeviceFlowStatusResponse(authenticated=False)
                
                # Optional: Check if token is actually valid by calling /user
                resp = await client.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"token {token}"}
                )
                if resp.status_code == 200:
                    user_data = resp.json()
                    return DeviceFlowStatusResponse(
                        authenticated=True, 
                        account=user_data.get("login")
                    )
        except Exception:
            pass
    return DeviceFlowStatusResponse(authenticated=False)

@router.post("/logout")
async def logout():
    """Clear the stored GitHub token."""
    if os.path.exists(settings.GITHUB_OAUTH_PATH):
        try:
            os.remove(settings.GITHUB_OAUTH_PATH)
            return {"status": "success"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to logout: {e}")
    return {"status": "success"}

async def save_token(data: dict):
    """Securely save the GitHub token to disk."""
    config_dir = os.path.dirname(settings.GITHUB_OAUTH_PATH)
    if not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)
    
    with open(settings.GITHUB_OAUTH_PATH, "w") as f:
        # Only save necessary fields
        token_data = {
            "access_token": data["access_token"],
            "token_type": data.get("token_type", "bearer"),
            "scope": data.get("scope", "")
        }
        json.dump(token_data, f, indent=2)
    logger.info(f"GitHub OAuth token saved to {settings.GITHUB_OAUTH_PATH}")
