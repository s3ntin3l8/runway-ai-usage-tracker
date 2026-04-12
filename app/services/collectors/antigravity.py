"""
Antigravity IDE quota collector with file-based data source.

Collection Strategy:
- Single Source: Local JSON quota file
  Antigravity IDE periodically writes quota data to ANTIGRAVITY_QUOTA_PATH
  Expected format: {"models": {"model_name": {"remaining_percent": X, "resets_at": timestamp}}}

Data Source:
- Location: Configured by ANTIGRAVITY_QUOTA_PATH (e.g., ~/.antigravity/quota.json)
- Updated by: Antigravity IDE when user checks quota or at startup
- Format: JSON with nested model usage data
- Fallback: Returns empty list if file missing or unreadable (allows other collectors to run)

Assumptions:
- remaining_percent: Already computed by IDE (0-100)
- resets_at: Unix timestamp in seconds when quota resets
- multiple models: Each model may have different quota windows

Error Handling:
- Missing file: Silently returns empty list (not critical)
- Invalid JSON: Silently returns empty list
- No models: Returns empty list (IDE may not be configured)
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
    def _fallback_strategies(self) -> List[Any]:
        """Return the strategy list for Antigravity."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect Antigravity quota via LSP probing (Primary) or local file (Fallback)."""
        if not settings.LOCAL_COLLECTOR_ENABLED:
            return []

        # Try LSP probe first (Preferred as per Gold Standard)
        lsp_res = await self._strategy_lsp(client)
        if lsp_res:
            return lsp_res
            
        # Fallback to local JSON file
        return await self._strategy_local_file(client)

    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return empty list on failure (Antigravity is non-critical)."""
        return []

    async def _strategy_lsp(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect Antigravity usage by probing the active language server processes.
        
        Strategy:
        1. Find language_server_macos processes via 'ps'.
        2. Detect all listening TCP ports for those PIDs via 'lsof'.
        3. Try potential CSRF tokens from CLI flags.
        4. Probe each port/token combination.
        """
        try:
            # 1. Detect processes and their tokens
            proc_info = await self._detect_lsp_proc_info()
            if not proc_info:
                return []

            results = []
            seen_services = set()

            for pid, tokens in proc_info.items():
                # 2. Find listening ports
                ports = await self._find_listening_ports(pid)

                # 3. Probe all port/token combinations in parallel
                probe_tasks = []
                for port in ports:
                    for csrf in tokens:
                        if not csrf: continue
                        probe_tasks.append(self._probe_lsp_service(client, port, csrf))

                if probe_tasks:
                    # Run all probes concurrently
                    probe_results = await asyncio.gather(*probe_tasks, return_exceptions=True)

                    for cards in probe_results:
                        if isinstance(cards, list):
                            for card in cards:
                                svc_key = f"{card['service']}_{card['remaining']}"
                                if svc_key not in seen_services:
                                    results.append(card)
                                    seen_services.add(svc_key)

            return results

        except Exception as e:
            logger.debug(f"Antigravity LSP strategy failed: {e}")
            return []

    async def _probe_lsp_service(self, client: httpx.AsyncClient, port: int, csrf: str) -> List[Dict[str, Any]]:
        """Probe a specific port/token for Antigravity status."""
        headers = {
            "X-Codeium-Csrf-Token": csrf,
            "Connect-Protocol-Version": "1",
            "Content-Type": "application/json",
        }
        
        payload = {
            "metadata": {
                "ideName": "antigravity",
                "extensionName": "antigravity",
                "ideVersion": "unknown",
                "locale": "en",
            }
        }

        # Try paths in order
        paths = [
            "/exa.language_server_pb.LanguageServerService/GetUserStatus",
            "/exa.language_server_pb.LanguageServerService/GetCommandModelConfigs"
        ]

        # Try HTTP only (since localhost is always HTTP for this service)
        for scheme in ["http"]:
            for path in paths:
                try:
                    url = f"{scheme}://127.0.0.1:{port}{path}"
                    # Use a short timeout since it's a local connection
                    resp = await client.post(url, headers=headers, json=payload, timeout=0.5)
                    
                    if resp.status_code == 200:
                        logger.info(f"✅ Antigravity LSP connected on port {port}")
                        data = resp.json()
                        return self._parse_lsp_response(data)
                    elif resp.status_code in (401, 403):
                        # Token might be wrong, skip this token/port combo
                        return []
                except Exception:
                    continue
        return []

    async def _detect_lsp_proc_info(self) -> Dict[int, List[str]]:
        """Find Antigravity PIDs and their associated CSRF tokens."""
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
                        # Capture all potential tokens
                        for pattern in [r"--csrf_token\s+([a-f0-9-]+)", r"--extension_server_csrf_token\s+([a-f0-9-]+)"]:
                            match = re.search(pattern, line)
                            if match: tokens.append(match.group(1))
                        
                        if tokens:
                            results[pid] = tokens
            return results
        except Exception as e:
            logger.debug(f"Failed to detect LSP processes: {e}")
            return {}

    async def _find_listening_ports(self, pid: int) -> List[int]:
        """Use lsof to find all listening TCP ports for a given PID."""
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
                # Extract port from e.g. "TCP 127.0.0.1:61641 (LISTEN)"
                match = re.search(r":(\d+)\s+\(LISTEN\)", line)
                if match:
                    port = int(match.group(1))
                    # Exclude our own application port
                    if port != settings.APP_PORT:
                        ports.append(port)
            return sorted(list(set(ports)))
        except Exception:
            return []

    def _parse_lsp_response(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse the binary/JSON Connect response into standardized quota cards."""
        results = []
        user_status = data.get("userStatus", {})
        
        # New structure parsing
        email = user_status.get("email", "")
        plan_info = user_status.get("planStatus", {}).get("planInfo", {})
        plan = plan_info.get("planName", "Standard")
        
        # Fallback to userTier if needed
        if plan == "Standard":
            plan = user_status.get("userTier", {}).get("name", "Standard")

        cascade_data = user_status.get("cascadeModelConfigData", {})
        configs = cascade_data.get("clientModelConfigs", [])

        identity_suffix = f" | {email}" if email else ""

        for config in configs:
            label = config.get("label", "").lower()
            quota = config.get("quotaInfo", {})
            rem_frac = quota.get("remainingFraction")
            
            if rem_frac is None:
                continue

            # Model mapping priority (Claude > Gemini Pro > Gemini Flash)
            icon = "🛸"
            service_name = config.get("label", "Unknown Model")
            
            if "claude" in label and "thinking" not in label:
                icon = "🟠"
                service_name = "AG: Claude"
            elif "gemini" in label:
                icon = "✨"
                if "pro" in label: service_name = "AG: Gemini Pro"
                elif "flash" in label: service_name = "AG: Gemini Flash"
            
            rem_pct = float(rem_frac) * 100
            reset_raw = quota.get("resetTime")
            reset_at = None
            if reset_raw:
                try:
                    # Handle ISO strings or numeric epochs
                    if isinstance(reset_raw, str):
                        reset_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                    else:
                        reset_at = datetime.fromtimestamp(float(reset_raw), tz=timezone.utc)
                except (ValueError, TypeError):
                    pass

            results.append({
                "service": service_name,
                "icon": icon,
                "remaining": f"{rem_pct:.1f}%",
                "unit": "capacity",
                "reset": human_delta(reset_at),
                "health": "good" if rem_pct > 30 else "warning",
                "pace": PaceCalculator.estimate_longevity(100 - rem_pct, reset_at),
                "detail": f"{plan}{identity_suffix} [LSP]",
                "reset_at": reset_at.isoformat() if reset_at else None,
                "tier": plan,
                "data_source": "lsp",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        # Parse AI Credits
        user_tier = user_status.get("userTier", {})
        credits_list = user_tier.get("availableCredits", [])
        
        for cred in credits_list:
            cred_type = cred.get("creditType", "UNKNOWN")
            amount_str = cred.get("creditAmount", "0")
            
            try:
                amount = float(amount_str)
            except (ValueError, TypeError):
                amount = 0.0

            # Mapping for credit types
            if cred_type == "GOOGLE_ONE_AI":
                label = "Google AI Credits"
            else:
                # Fallback: SNAKE_CASE to Title Case
                label = cred_type.replace("_", " ").title() + " Credits"
            
            results.append({
                "service": f"AG: {label}",
                "icon": "💰",
                "remaining": f"{amount:,.0f}",
                "unit": "credits",
                "reset": "Prepaid",
                "health": "good" if amount >= 100 else "warning",
                "pace": "Stable",
                "detail": f"{label} balance [LSP]",
                "used_value": 0.0,
                "limit_value": amount,
                "unit_type": "generic",
                "data_source": "lsp",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "tier": plan,
            })

        return results

    async def _strategy_local_file(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect Antigravity quota from local JSON file (Fallback)."""
        path = settings.ANTIGRAVITY_QUOTA_PATH
        try:
            with open(path, "r") as f:
                data = json.load(f)
            res = []
            for name, usage in data.get("models", {}).items():
                rem = usage.get("remaining_percent", 0.0)
                reset = (
                    datetime.fromtimestamp(usage["resets_at"], tz=timezone.utc)
                    if "resets_at" in usage
                    else None
                )
                res.append(
                    {
                        "service": f"AG: {name}",
                        "icon": "🛸",
                        "remaining": f"{rem:.1f}%",
                        "unit": "remaining",
                        "reset": human_delta(reset),
                        "health": "good" if rem > 30 else "warning",
                        "pace": PaceCalculator.estimate_longevity(100 - rem, reset),
                        "detail": f"{name} [IDE/File]",
                        "reset_at": reset.isoformat() if reset else None,
                        "data_source": "local_file",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            return res
        except (FileNotFoundError, PermissionError, json.JSONDecodeError, KeyError, ValueError):
            return []

