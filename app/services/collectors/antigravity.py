"""
Antigravity IDE quota collector with file-based data source.
"""

import re
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
import httpx
from app.core.config import settings
from app.core.utils import PaceCalculator, human_delta
from app.services.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


class AntigravityCollector(BaseCollector):
    PROVIDER_ID = "antigravity"
    DEFAULT_WINDOW_TYPE = "session"

    def __init__(self, account_id: Optional[str] = None, account_label: Optional[str] = None):
        super().__init__(account_id=account_id, account_label=account_label)

    def _fallback_strategies(self) -> List[Any]:
        """Return the strategy list for Antigravity."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect Antigravity quota via LSP probing (Primary) or local file (Fallback)."""
        if not settings.LOCAL_COLLECTOR_ENABLED:
            return []

        # Try LSP probe first
        lsp_res = await self._strategy_lsp(client)
        if lsp_res:
            return lsp_res
            
        # Fallback to local JSON file
        return await self._strategy_local_file(client)

    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return empty list on failure."""
        return []

    async def _strategy_lsp(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Probes the active language server processes."""
        try:
            proc_info = await self._detect_lsp_proc_info()
            if not proc_info: return []

            results = []
            seen_services = set()

            for pid, tokens in proc_info.items():
                ports = await self._find_listening_ports(pid)
                probe_tasks = []
                for port in ports:
                    for csrf in tokens:
                        if not csrf: continue
                        probe_tasks.append(self._probe_lsp_service(client, port, csrf))

                if probe_tasks:
                    probe_results = await asyncio.gather(*probe_tasks, return_exceptions=True)
                    for cards in probe_results:
                        if isinstance(cards, list):
                            for card in cards:
                                svc_key = f"{card['service_name']}_{card['remaining']}"
                                if svc_key not in seen_services:
                                    results.append(card)
                                    seen_services.add(svc_key)
            return results
        except Exception: return []

    async def _probe_lsp_service(self, client: httpx.AsyncClient, port: int, csrf: str) -> List[Dict[str, Any]]:
        """Probe a specific port/token."""
        headers = {
            "X-Codeium-Csrf-Token": csrf,
            "Connect-Protocol-Version": "1",
            "Content-Type": "application/json",
        }
        payload = {"metadata": {"ideName": "antigravity", "extensionName": "antigravity"}}

        try:
            url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus"
            resp = await client.post(url, headers=headers, json=payload, timeout=0.5)
            if resp.status_code == 200:
                return self._parse_lsp_response(resp.json())
        except Exception: pass
        return []

    async def _detect_lsp_proc_info(self) -> Dict[int, List[str]]:
        """Find Antigravity PIDs and tokens."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-ax", "-o", "pid,command",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode(errors="ignore")
            results = {}
            for line in output.splitlines():
                if "language_server_macos" in line:
                    pid_match = re.search(r"^\s*(\d+)", line)
                    if pid_match:
                        pid = int(pid_match.group(1))
                        tokens = []
                        for pattern in [r"--csrf_token\s+([a-f0-9-]+)", r"--extension_server_csrf_token\s+([a-f0-9-]+)"]:
                            match = re.search(pattern, line)
                            if match: tokens.append(match.group(1))
                        if tokens: results[pid] = tokens
            return results
        except Exception: return {}

    async def _find_listening_ports(self, pid: int) -> List[int]:
        """Find listening ports for PID."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-p", str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode(errors="ignore")
            ports = []
            for line in output.splitlines():
                match = re.search(r":(\d+)\s+\(LISTEN\)", line)
                if match:
                    port = int(match.group(1))
                    if port != settings.APP_PORT: ports.append(port)
            return sorted(list(set(ports)))
        except Exception: return []

    def _parse_lsp_response(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse LSP response."""
        results = []
        user_status = data.get("userStatus", {})
        email = user_status.get("email", "")
        
        # Identity Promotion
        if email and self.account_id:
            from app.services.token_cache import token_cache
            asyncio.create_task(token_cache.update_account_metadata("antigravity", self.account_id, name=email))
            self.account_label = email

        plan_info = user_status.get("planStatus", {}).get("planInfo", {})
        plan = plan_info.get("planName", "Standard")
        cascade_data = user_status.get("cascadeModelConfigData", {})
        configs = cascade_data.get("clientModelConfigs", [])

        for config in configs:
            label = config.get("label", "").lower()
            quota = config.get("quotaInfo", {})
            rem_frac = quota.get("remainingFraction")
            if rem_frac is None: continue

            service_name = config.get("label", "Unknown Model")
            rem_pct = float(rem_frac) * 100
            
            results.append({
                "service_name": f"AG: {service_name}",
                "icon": "🛸",
                "remaining": f"{rem_pct:.1f}%",
                "unit": "capacity",
                "reset": "Dynamic",
                "pace": "Continuous",
                "health": "good" if rem_pct > 30 else "warning",
                "detail": f"{plan} | {email} [LSP]",
                "tier": plan,
                "data_source": "lsp",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        # Process Credits
        credits_data = user_status.get("userTier", {}).get("availableCredits", [])
        for cred in credits_data:
            c_type = cred.get("creditType", "AI Credits")
            amount = cred.get("creditAmount", "0")
            
            # Map types to human names
            name_map = {
                "GOOGLE_ONE_AI": "Google AI Credits",
                "ANTHROPIC_CREDIT": "Anthropic Credits",
            }
            display_name = name_map.get(c_type, c_type.replace("_", " ").title())
            
            results.append({
                "service_name": f"AG: {display_name}",
                "icon": "💰",
                "remaining": amount,
                "unit": "credits",
                "reset": "Prepaid",
                "pace": "N/A",
                "health": "good" if int(amount) > 100 else "warning",
                "detail": f"{display_name} | {email} [LSP]",
                "data_source": "lsp",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        return results

    async def _strategy_local_file(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect Antigravity quota from local JSON file."""
        path = settings.ANTIGRAVITY_QUOTA_PATH
        try:
            with open(path, "r") as f:
                data = json.load(f)
            res = []
            for name, usage in data.get("models", {}).items():
                rem = usage.get("remaining_percent", 0.0)
                res.append({
                    "service_name": f"AG: {name}",
                    "icon": "🛸",
                    "remaining": f"{rem:.1f}%",
                    "unit": "remaining",
                    "reset": "Unknown",
                    "pace": "N/A",
                    "health": "good" if rem > 30 else "warning",
                    "detail": f"{name} [IDE/File]",
                    "data_source": "local_file",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
            return res
        except Exception: return []
