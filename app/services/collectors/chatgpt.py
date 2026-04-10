"""
ChatGPT Codex quota collector with API and local cache fallback.

Collection Strategy:
1. Primary: ChatGPT wham/usage API endpoint
   - Requires OAuth token from environment (CHATGPT_OAUTH_TOKEN) or ~/.codex/auth.json
   - Falls back to Web Dashboard Scraping (Browser Cookies) if no token is found
   - Calls https://chatgpt.com/backend-api/wham/usage (requires Bearer auth)
   - Returns utilization percentage and reset timestamp

2. Authentication Priority:
   - Priority 1: CHATGPT_OAUTH_TOKEN environment variable
   - Priority 2: ~/.codex/auth.json (Codex CLI auth cache)
   - Priority 3: Browser Cookies (__Secure-next-auth.session-token)
   - Priority 4: Sidecar cache (forwarded tokens/cookies from other hosts)

3. Fallback: Codex CLI RPC (Fidelity Fallback)
   - Launches local RPC server: codex -s read-only -a untrusted app-server
   - Communicates via JSON-RPC over stdin/stdout
   - Provides account identity, plan type, usage windows, and credits

4. Fallback: Local session cache
   - Parses CHATGPT_SESSIONS_DIR for .jsonl session files
   - Uses most recently modified file (represents latest session)
   - Reads last line of log file for cached usage snapshot

5. Error Handling:
   - No auth: Returns "No logs/auth" error
   - API failure: Falls back to CLI RPC, then local logs
   - All fail: Returns API error or parse error card
"""

import os
import uuid
import glob
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import asyncio
import httpx
from app.core.config import settings
from app.services.credential_provider import credential_provider
from app.core.utils import PaceCalculator, human_delta, error_card
from app.core.browser_cookies import get_chatgpt_session_token, get_chatgpt_device_id
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class ChatGPTCollector(BaseCollector):
    def __init__(self):
        """Initialize caching for API results and refreshed tokens."""
        self._cached_api_results = None
        self._last_api_fetch = None
        self._cache_ttl = 300  # 5 minutes cache for lighter rate limits

        # Token refresh cache
        self._refreshed_token = None
        self._refreshed_token_expiry = None
        
        # Persistent device ID for session
        self._device_id = str(uuid.uuid4())

    async def _get_auth_data(self, client: httpx.AsyncClient) -> Dict[str, Any]:
        """
        Retrieve ChatGPT auth with priority: OAUTH -> Browser Cookies -> Sidecar Cache.
        """
        # Priority 1 & 2: Env var or auth.json (Centralized in CredentialProvider)
        auth_data = credential_provider.get_chatgpt_data()
        token = auth_data.get("access_token")
        account_id = auth_data.get("account_id")
        refresh_token = auth_data.get("refresh_token")

        if token:
            # Check if we need to refresh the OAuth token (if it's from auth.json and stale)
            last_refresh = auth_data.get("last_refresh")
            if last_refresh and refresh_token:
                try:
                    lr_dt = datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - lr_dt).days >= 8:
                        logger.info("ChatGPT OAuth token is stale (8+ days), refreshing...")
                        new_tokens = await self._refresh_oauth_token(client, refresh_token)
                        if new_tokens:
                            token = new_tokens["access_token"]
                except Exception as e:
                    logger.debug(f"Failed to check/refresh stale ChatGPT token: {e}")

            return {
                "token": token, 
                "account_id": account_id,
                "refresh_token": refresh_token,
                "source": "credential_provider"
            }

        # Priority 3: Browser Cookies
        session_token = await asyncio.to_thread(get_chatgpt_session_token)
        if not session_token:
            # Check sidecar cache for cookie
            session_token = await token_cache.get_token(
                "chatgpt", "cookie___Secure-next-auth.session-token"
            )

        if session_token:
            # Try to get refreshed token from in-memory cache
            now = datetime.now(timezone.utc)
            if (
                self._refreshed_token
                and self._refreshed_token_expiry
                and now < self._refreshed_token_expiry
            ):
                return {"token": self._refreshed_token, "source": "cookies_cached"}

            # Refresh Bearer token using session cookie
            refreshed = await self._refresh_access_token(client, session_token)
            if refreshed:
                self._refreshed_token = refreshed
                self._refreshed_token_expiry = now + timedelta(hours=1)
                return {"token": refreshed, "source": "cookies"}

        # Priority 4: Sidecar cache (direct OAuth token)
        token = await token_cache.get_token("chatgpt", "oauth_token")
        if token:
            logger.debug("Using OAuth token from sidecar cache")
            return {"token": token, "source": "sidecar_cache"}

        return {}

    async def _refresh_oauth_token(
        self, client: httpx.AsyncClient, refresh_token: str
    ) -> Optional[Dict[str, str]]:
        """Refresh OAuth token using the OpenAI auth endpoint."""
        try:
            resp = await client.post(
                "https://auth.openai.com/oauth/token",
                json={
                    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "openid profile email",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                new_data = {
                    "access_token": data.get("access_token"),
                    "refresh_token": data.get("refresh_token"),
                    "id_token": data.get("id_token"),
                }
                # Persist to disk if possible
                await self._save_refreshed_oauth_token(new_data)
                return new_data
        except Exception as e:
            logger.debug(f"Error refreshing ChatGPT OAuth token: {e}")
        return None

    async def _save_refreshed_oauth_token(self, data: Dict[str, str]):
        """Persist refreshed OAuth tokens back to auth.json."""
        if not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
            return

        auth_path = settings.CHATGPT_AUTH_PATH
        if not os.path.exists(auth_path):
            return

        try:
            # Read existing
            with open(auth_path, "r") as f:
                existing = json.load(f)

            # Update
            existing["tokens"]["access_token"] = data["access_token"]
            if data.get("refresh_token"):
                existing["refresh_token"] = data["refresh_token"]
            if data.get("id_token"):
                existing["id_token"] = data["id_token"]
            existing["last_refresh"] = datetime.now(timezone.utc).isoformat()

            # Write back
            with open(auth_path, "w") as f:
                json.dump(existing, f, indent=2)
            logger.info(f"Updated ChatGPT OAuth tokens in {auth_path}")
        except Exception as e:
            logger.debug(f"Failed to persist refreshed ChatGPT token: {e}")

    async def _get_device_id(self) -> str:
        """Get device ID from cookies or use generated session ID."""
        cookie_id = await asyncio.to_thread(get_chatgpt_device_id)
        return cookie_id if cookie_id else self._device_id

    async def _refresh_access_token(
        self, client: httpx.AsyncClient, session_token: str
    ) -> Optional[str]:
        """Exchange session cookie for a Bearer accessToken."""
        try:
            url = "https://chatgpt.com/api/auth/session"
            headers = {
                "Cookie": f"__Secure-next-auth.session-token={session_token}",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://chatgpt.com/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "oai-device-id": await self._get_device_id(),
                "oai-language": "en-US",
                "Priority": "u=1, i",
            }
            resp = await client.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("accessToken")
            else:
                logger.debug(
                    f"Failed to refresh ChatGPT token: HTTP {resp.status_code}"
                )
        except Exception as e:
            logger.debug(f"Error refreshing ChatGPT token: {e}")
        return None

    async def _collect_via_cli_rpc(self) -> List[Dict[str, Any]]:
        """
        Fetch usage data from the codex CLI RPC server.
        Mimics a robust gold standard collection strategy.
        """
        process = None
        try:
            # Launch local RPC server in read-only, untrusted mode
            process = await asyncio.create_subprocess_exec(
                "codex", "-s", "read-only", "-a", "untrusted", "app-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )

            async def call_rpc(method: str, params: Optional[Dict] = None) -> Optional[Dict]:
                if not process.stdin: return None
                request = {
                    "jsonrpc": "2.0",
                    "id": str(uuid.uuid4()),
                    "method": method,
                    "params": params or {}
                }
                process.stdin.write((json.dumps(request) + "\n").encode())
                await process.stdin.drain()
                
                # Read response line
                line = await process.stdout.readline()
                if not line: return None
                try:
                    response = json.loads(line.decode())
                    logger.debug(f"RPC {method} response: {response}")
                    return response.get("result")
                except json.JSONDecodeError:
                    return None

            # 1. Initialize (requires clientInfo)
            init_res = await call_rpc("initialize", {"clientInfo": {"name": "Runway", "version": "1.0.0"}})
            if not init_res:
                return []

            # 2. Get Account Info
            account_data = await call_rpc("account/read")
            account = account_data.get("account") if account_data else None
            
            # 3. Get Rate Limits
            limits_data = await call_rpc("account/rateLimits/read")
            limits = limits_data.get("rateLimits") if limits_data else None
            
            if not limits:
                return []

            cards = []
            now = datetime.now(timezone.utc)
            
            # Process Tier
            tier = "free"
            email = "Unknown"
            if account:
                plan_type = account.get("planType", "").lower()
                if "plus" in plan_type or "pro" in plan_type: tier = "plus"
                elif "team" in plan_type: tier = "team"
                email = account.get("email", "Unknown")
                
                status = "Active"
                cards.append({
                    "service": "ChatGPT Account",
                    "icon": "💬",
                    "remaining": tier.upper(),
                    "unit": "tier",
                    "reset": status,
                    "health": "good",
                    "pace": "Active",
                    "detail": f"Account: {email} [CLI RPC]",
                    "data_source": "cli",
                    "tier": tier,
                    "updated_at": now.isoformat(),
                })

            # Process Windows
            primary = limits.get("primary")
            if primary:
                pct = float(primary.get("usedPercent", 0.0))
                reset_ts = primary.get("resetsAt")
                # Codex returns epoch seconds for resetsAt
                reset_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc) if reset_ts else None
                
                cards.append({
                    "service": "ChatGPT Codex",
                    "icon": "💬",
                    "remaining": f"{(100-pct):.1f}%",
                    "unit": "remaining",
                    "reset": human_delta(reset_at),
                    "health": "good" if pct < 80 else "warning",
                    "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                    "detail": f"{pct:.1f}% used [CLI RPC]",
                    "used_value": pct,
                    "limit_value": 100.0,
                    "unit_type": "percent",
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": "cli",
                    "tier": tier,
                    "usage_url": "https://chatgpt.com/codex/settings/usage/",
                    "updated_at": now.isoformat(),
                })

            # Process Credits
            credits = limits.get("credits")
            if credits and not credits.get("unlimited", False):
                balance = credits.get("balance")
                if balance is not None:
                    balance = float(balance)
                    cards.append({
                        "service": "ChatGPT Credits",
                        "icon": "💰",
                        "remaining": f"${balance:.2f}",
                        "unit": "balance",
                        "reset": "Prepaid",
                        "health": "good" if balance > 5 else "warning",
                        "pace": "Stable",
                        "detail": f"Credits: ${balance:.2f} [CLI RPC]",
                        "used_value": 0.0,
                        "limit_value": balance,
                        "is_unlimited": False,
                        "unit_type": "currency",
                        "currency": "USD",
                        "data_source": "cli",
                        "tier": tier,
                        "updated_at": now.isoformat(),
                    })

            return cards

        except Exception as e:
            logger.debug(f"Codex CLI RPC failed with error: {e}", exc_info=True)
            return []
        finally:
            if process:
                try:
                    process.terminate()
                    await process.wait()
                except:
                    pass

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect ChatGPT Codex quota and Account info using API with caching and local fallback.
        """
        auth = await self._get_auth_data(client)
        token = auth.get("token")
        account_id = auth.get("account_id")

        # Check API cache first
        now = datetime.now(timezone.utc)
        cached_error = None
        if self._cached_api_results is not None and self._last_api_fetch:
            if (now - self._last_api_fetch).total_seconds() < self._cache_ttl:
                if not self._is_error_result(self._cached_api_results):
                    return self._cached_api_results
                cached_error = self._cached_api_results

        # 1. Try API if we have a token
        if token:
            skip_api = False
            if cached_error:
                error_age = (now - self._last_api_fetch).total_seconds()
                if error_age < 60:
                    skip_api = True

            if not skip_api:
                try:
                    headers = {
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                        "Accept": "*/*",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://chatgpt.com/",
                        "Origin": "https://chatgpt.com",
                        "Sec-Fetch-Dest": "empty",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Site": "same-origin",
                        "oai-device-id": await self._get_device_id(),
                        "oai-language": "en-US",
                        "Priority": "u=1, i",
                    }
                    if account_id:
                        headers["ChatGPT-Account-Id"] = account_id
                    
                    cards = []
                    tier = "free"

                    # 1. Fetch Account Info (Tier, Credits) — non-fatal if 403
                    account_url = "https://chatgpt.com/backend-api/accounts/check/v4"
                    try:
                        acc_resp = await client.get(account_url, headers=headers, timeout=10)
                        if acc_resp.status_code == 401 or acc_resp.status_code == 403:
                            logger.info(f"ChatGPT accounts/check returned {acc_resp.status_code}, skipping account card")
                        else:
                            acc_resp.raise_for_status()
                            acc_data = acc_resp.json()
                            account_list = acc_data.get("accounts", {})
                            primary_account = (
                                next(iter(account_list.values()), {}) if account_list else {}
                            )
                            entitlements = primary_account.get("entitlements", [])

                            if any(e.get("slug") == "plus" for e in entitlements):
                                tier = "plus"
                            elif any(e.get("slug") == "team" for e in entitlements):
                                tier = "team"

                            status = primary_account.get("account_status", "active")
                            cards.append(
                                {
                                    "service": "ChatGPT Account",
                                    "icon": "💬",
                                    "remaining": tier.upper(),
                                    "unit": "tier",
                                    "reset": status.capitalize(),
                                    "health": "good",
                                    "pace": "Active" if status == "active" else "Alert",
                                    "detail": f"Account Tier: {tier.capitalize()} · {status}",
                                    "data_source": auth.get("source", "oauth"),
                                    "tier": tier,
                                    "updated_at": now.isoformat(),
                                }
                            )
                    except Exception as e:
                        logger.debug(f"ChatGPT accounts/check failed (non-fatal): {e}")

                    # 2. Fetch Usage (wham/usage)
                    usage_url = "https://chatgpt.com/backend-api/wham/usage"
                    resp = await client.get(usage_url, headers=headers, timeout=10)
                    if resp.status_code == 401 or resp.status_code == 403:
                        # Token may be expired — attempt refresh and retry once
                        session_token = get_chatgpt_session_token()
                        if session_token:
                            refreshed = await self._refresh_access_token(client, session_token)
                            if refreshed:
                                self._refreshed_token = refreshed
                                self._refreshed_token_expiry = now + timedelta(hours=1)
                                headers["Authorization"] = f"Bearer {refreshed}"
                                resp = await client.get(usage_url, headers=headers, timeout=10)
                    resp.raise_for_status()

                    data = resp.json()
                    rate_limit = data.get("rate_limit", {})
                    primary = rate_limit.get("primary_window", {})
                    if primary:
                        pct = primary.get("used_percent", 0.0)
                        reset_ts = primary.get("reset_at")
                        reset_at = (
                            datetime.fromtimestamp(reset_ts, tz=timezone.utc)
                            if reset_ts
                            else None
                        )

                        cards.append(
                            {
                                "service": "ChatGPT Codex",
                                "icon": "💬",
                                "remaining": f"{(100-pct):.1f}%",
                                "unit": "remaining",
                                "reset": human_delta(reset_at),
                                "health": "good" if pct < 80 else "warning",
                                "pace": PaceCalculator.estimate_longevity(
                                    pct, reset_at
                                ),
                                "detail": f"{pct:.1f}% used",
                                "used_value": float(pct),
                                "limit_value": 100.0,
                                "unit_type": "percent",
                                "reset_at": reset_at.isoformat() if reset_at else None,
                                "data_source": auth.get("source", "oauth"),
                                "tier": tier,
                                "usage_url": "https://chatgpt.com/codex/settings/usage/",
                                "updated_at": now.isoformat(),
                            }
                        )

                    if cards:
                        self._cached_api_results = cards
                        self._last_api_fetch = now
                        return cards

                except Exception as e:
                    logger.debug(f"ChatGPT Web API failed, trying CLI RPC: {e}")
                    # Track this error in case fallbacks also fail
                    cached_error = [
                        error_card(
                            "ChatGPT Codex",
                            "💬",
                            f"API Error: {str(e)[:20]}",
                            error_type="api_error",
                        )
                    ]
                    # Don't return yet, fall through to RPC

        # 2. Try Codex CLI RPC (Fidelity Fallback)
        if settings.LOCAL_COLLECTOR_ENABLED:
            rpc_res = await self._collect_via_cli_rpc()
            if rpc_res:
                self._cached_api_results = rpc_res
                self._last_api_fetch = now
                return rpc_res

        # 3. Fallback to local logs
        if settings.LOCAL_COLLECTOR_ENABLED:
            path = settings.CHATGPT_SESSIONS_DIR
            try:
                # Wrap blocking glob and stat calls in thread
                files = await asyncio.to_thread(
                    glob.glob, f"{path}/**/*.jsonl", recursive=True
                )
                if not files:
                    # If we have an API error (either from this call or cached), prefer returning that
                    final_result = cached_error or [
                        error_card(
                            "ChatGPT Codex",
                            "💬",
                            "No logs/auth",
                            error_type="missing_config",
                        )
                    ]
                    self._cached_api_results = final_result
                    self._last_api_fetch = now
                    return final_result

                latest = await asyncio.to_thread(max, files, key=os.path.getmtime)

                def read_last_line(file_path):
                    last_line = None
                    with open(file_path, "r") as f:
                        for line in f:
                            if line.strip():
                                last_line = line
                    return last_line

                last_line = await asyncio.to_thread(read_last_line, latest)

                if not last_line:
                    final_result = [
                        error_card(
                            "ChatGPT Codex", "💬", "Empty log", error_type="parse_error"
                        )
                    ]
                    self._cached_api_results = final_result
                    self._last_api_fetch = now
                    return final_result
                
                usage = json.loads(last_line)
                pct = usage.get("used_percent", 0.0)
                reset_at = (
                    datetime.fromtimestamp(usage["resets_at"], tz=timezone.utc)
                    if "resets_at" in usage
                    else None
                )

                final_result = [
                    {
                        "service": "ChatGPT Codex",
                        "icon": "💬",
                        "remaining": f"{(100-pct):.1f}%",
                        "unit": "remaining",
                        "reset": human_delta(reset_at),
                        "health": "good" if pct < 80 else "warning",
                        "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                        "detail": f"{pct:.1f}% used",
                        "used_value": float(pct),
                        "limit_value": 100.0,
                        "unit_type": "percent",
                        "reset_at": reset_at.isoformat() if reset_at else None,
                        "data_source": "cache",
                        "usage_url": "https://chatgpt.com/codex/settings/usage/",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
                self._cached_api_results = final_result
                self._last_api_fetch = now
                return final_result
            except Exception as e:
                logger.debug(f"ChatGPT Fallback Error: {e}")
                final_result = [
                    error_card(
                        "ChatGPT Codex", "💬", "Parse Error", error_type="parse_error"
                    )
                ]
                self._cached_api_results = final_result
                self._last_api_fetch = now
                return final_result

        # If local collector is disabled and API failed, return cached error or default error
        final_result = cached_error or [
            error_card(
                "ChatGPT Codex", "💬", "No auth data", error_type="missing_config"
            )
        ]
        self._cached_api_results = final_result
        self._last_api_fetch = now
        return final_result
