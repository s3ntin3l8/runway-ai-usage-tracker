import os
import json
import asyncio
import logging
import glob
import uuid
from typing import List, Dict, Any, Optional
import httpx
from datetime import datetime, timezone
from app.core.config import settings
from app.core.utils import PaceCalculator, human_delta

logger = logging.getLogger(__name__)

class ChatGPTLocalMixin:
    """Mixin for ChatGPT local session and CLI RPC collection."""
    
    async def _collect_via_cli_rpc(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, Any]]:
        """
        Fetch usage data from the codex CLI RPC server.
        """
        process = None
        try:
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
                
                line = await process.stdout.readline()
                if not line: return None
                try:
                    response = json.loads(line.decode())
                    return response.get("result")
                except json.JSONDecodeError:
                    return None

            init_res = await call_rpc("initialize", {"clientInfo": {"name": "Runway", "version": "1.0.0"}})
            if not init_res:
                return []

            account_data = await call_rpc("account/read")
            account = account_data.get("account") if account_data else None
            
            limits_data = await call_rpc("account/rateLimits/read")
            limits = limits_data.get("rateLimits") if limits_data else None
            
            if not limits:
                return []

            cards = []
            now = datetime.now(timezone.utc)
            
            tier = "free"
            email = "Unknown"
            if account:
                plan_type = account.get("planType", "").lower()
                if "plus" in plan_type or "pro" in plan_type: tier = "plus"
                elif "team" in plan_type: tier = "team"
                email = account.get("email", "Unknown")
                
                cards.append({
                    "service_name": "ChatGPT Account",
                    "icon": "💬",
                    "remaining": tier.upper(),
                    "unit": "tier",
                    "reset": "Active",
                    "health": "good",
                    "pace": "Active",
                    "detail": f"Account: {email} [CLI RPC]",
                    "data_source": "cli",
                    "tier": tier,
                    "updated_at": now.isoformat(),
                })

            primary = limits.get("primary")
            if primary:
                pct = float(primary.get("usedPercent", 0.0))
                reset_ts = primary.get("resetsAt")
                reset_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc) if reset_ts else None
                
                cards.append({
                    "service_name": "ChatGPT Codex",
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
                })

            credits = limits.get("credits")
            if credits:
                balance = credits.get("balance", 0.0)
                cards.append({
                    "service_name": "ChatGPT Credits",
                    "icon": "💰",
                    "remaining": f"${balance:.2f}",
                    "unit": "USD",
                    "reset": "Prepaid",
                    "health": "good",
                    "pace": "N/A",
                    "detail": f"Balance: ${balance:.2f} [CLI RPC]",
                    "data_source": "cli",
                    "tier": tier,
                    "updated_at": now.isoformat(),
                })

            return cards

        except Exception as e:
            logger.debug(f"Codex CLI RPC failed: {e}")
            return []
        finally:
            if process:
                try:
                    process.terminate()
                    await process.wait()
                except (ProcessLookupError, OSError):
                    pass

    async def _strategy_local_logs(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Local log parsing fallback."""
        if not settings.LOCAL_COLLECTOR_ENABLED: return []
        path = settings.CHATGPT_SESSIONS_DIR
        try:
            files = await asyncio.to_thread(glob.glob, f"{path}/**/*.jsonl", recursive=True)
            if not files: return []
            latest = await asyncio.to_thread(max, files, key=os.path.getmtime)
            
            with open(latest, "r") as f:
                lines = f.readlines()
                if not lines: return []
                usage = json.loads(lines[-1])
            
            pct = usage.get("used_percent", 0.0)
            reset_at = datetime.fromtimestamp(usage["resets_at"], tz=timezone.utc) if "resets_at" in usage else None

            return [{
                "service_name": "ChatGPT Codex",
                "icon": "💬",
                "remaining": f"{(100-pct):.1f}%",
                "unit": "remaining",
                "reset": human_delta(reset_at),
                "detail": f"{pct:.1f}% used",
                "data_source": "cache",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }]
        except Exception: return []
