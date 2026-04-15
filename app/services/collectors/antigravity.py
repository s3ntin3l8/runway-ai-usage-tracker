"""
Antigravity IDE quota collector with file-based data source.
"""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import is_local_collector_enabled, settings
from app.services.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


def _format_reset(unix_ts: int | float | None) -> tuple[str, str | None]:
    """Convert a Unix timestamp to (human-readable display, ISO 8601 string).

    Returns ("Dynamic", None) when timestamp is absent or invalid.
    """
    if not unix_ts:
        return "Dynamic", None
    try:
        dt = datetime.fromtimestamp(float(unix_ts), UTC)
        reset_at = dt.isoformat()
        seconds = int((dt - datetime.now(UTC)).total_seconds())
        if seconds < 0:
            return "Expired", reset_at
        if seconds < 3600:
            return f"in {seconds // 60}m", reset_at
        return f"in {seconds // 3600}h {(seconds % 3600) // 60}m", reset_at
    except Exception:
        return "Dynamic", None


class AntigravityCollector(BaseCollector):
    PROVIDER_ID = "antigravity"
    DEFAULT_WINDOW_TYPE = "session"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)

    def _fallback_strategies(self) -> list[Any]:
        """Return the strategy list for Antigravity."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect Antigravity quota via LSP probing (Primary) or local file (Fallback)."""
        if not is_local_collector_enabled():
            return []

        # Try LSP probe first
        lsp_res = await self._strategy_lsp(client)
        if lsp_res:
            return lsp_res

        # Fallback to local JSON file
        return await self._strategy_local_file(client)

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return empty list on failure."""
        return []

    async def _strategy_lsp(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Probes the active language server processes."""
        try:
            proc_info = await self._detect_lsp_proc_info()
            if not proc_info:
                return []

            results = []
            seen_services = set()

            for pid, tokens in proc_info.items():
                ports = await self._find_listening_ports(pid)
                probe_tasks = []
                for port in ports:
                    for csrf in tokens:
                        if not csrf:
                            continue
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
        except Exception:
            return []

    async def _probe_lsp_service(
        self, client: httpx.AsyncClient, port: int, csrf: str
    ) -> list[dict[str, Any]]:
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
        except Exception:
            pass
        return []

    async def _detect_lsp_proc_info(self) -> dict[int, list[str]]:
        """Find Antigravity PIDs and tokens."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps",
                "-ax",
                "-o",
                "pid,command",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
                        for pattern in [
                            r"--csrf_token\s+([a-f0-9-]+)",
                            r"--extension_server_csrf_token\s+([a-f0-9-]+)",
                        ]:
                            match = re.search(pattern, line)
                            if match:
                                tokens.append(match.group(1))
                        if tokens:
                            results[pid] = tokens
            return results
        except Exception:
            return {}

    async def _find_listening_ports(self, pid: int) -> list[int]:
        """Find listening ports for PID."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof",
                "-nP",
                "-iTCP",
                "-sTCP:LISTEN",
                "-p",
                str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode(errors="ignore")
            ports = []
            for line in output.splitlines():
                match = re.search(r":(\d+)\s+\(LISTEN\)", line)
                if match:
                    port = int(match.group(1))
                    if port != settings.APP_PORT:
                        ports.append(port)
            return sorted(list(set(ports)))
        except Exception:
            return []

    def _parse_lsp_response(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse LSP response."""
        results = []
        user_status = data.get("userStatus", {})
        email = user_status.get("email", "")

        # Identity Promotion
        if email and self.account_id:
            from app.services.token_cache import token_cache

            asyncio.create_task(
                token_cache.update_account_metadata("antigravity", self.account_id, name=email)
            )
            self.account_label = email

        plan_info = user_status.get("planStatus", {}).get("planInfo", {})
        plan = plan_info.get("planName", "Standard")
        cascade_data = user_status.get("cascadeModelConfigData", {})
        configs = cascade_data.get("clientModelConfigs", [])

        for config in configs:
            quota = config.get("quotaInfo", {})
            rem_frac = quota.get("remainingFraction")
            if rem_frac is None:
                continue

            label = config.get("label", "Unknown Model")
            model_id = config.get("modelOrAlias", label)
            rem_pct = float(rem_frac) * 100
            reset_display, reset_at = _format_reset(quota.get("resetTime"))

            results.append(
                {
                    "service_name": label,
                    "icon": "🛸",
                    "remaining": f"{rem_pct:.1f}%",
                    "unit": "capacity",
                    "reset": reset_display,
                    "pace": "Continuous",
                    "health": "good" if rem_pct > 30 else "warning",
                    "detail": f"{plan} | {email} [LSP]",
                    "tier": plan,
                    "data_source": "lsp",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "provider_id": "antigravity",
                    "account_label": email or None,
                    "model_id": model_id,
                    "used_value": round(100.0 - rem_pct, 4),
                    "limit_value": 100.0,
                    "unit_type": "percent",
                    "window_type": "session",
                    "reset_at": reset_at,
                }
            )

        # Process Credits
        credits_data = user_status.get("userTier", {}).get("availableCredits", [])
        for cred in credits_data:
            c_type = cred.get("creditType", "AI Credits")
            amount = str(cred.get("creditAmount", "0"))

            # Map types to human names
            name_map = {
                "GOOGLE_ONE_AI": "Google AI Credits",
                "ANTHROPIC_CREDIT": "Anthropic Credits",
            }
            display_name = name_map.get(c_type, c_type.replace("_", " ").title())

            try:
                health = "good" if int(amount) > 100 else "warning"
            except ValueError:
                health = "warning"

            results.append(
                {
                    "service_name": display_name,
                    "icon": "💰",
                    "remaining": amount,
                    "unit": "credits",
                    "reset": "Prepaid",
                    "pace": "N/A",
                    "health": health,
                    "detail": f"{display_name} | {email} [LSP]",
                    "data_source": "lsp",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "provider_id": "antigravity",
                    "account_label": email or None,
                    "used_value": None,
                    "limit_value": None,
                    "unit_type": "credits",
                    "window_type": "session",
                }
            )
        return results

    async def _strategy_local_file(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect Antigravity quota from local JSON file."""
        path = settings.ANTIGRAVITY_QUOTA_PATH
        try:
            with open(path) as f:
                data = json.load(f)
            res = []
            for name, usage in data.get("models", {}).items():
                rem = usage.get("remaining_percent", 0.0)
                reset_display, reset_at = _format_reset(usage.get("resets_at"))
                res.append(
                    {
                        "service_name": name,
                        "icon": "🛸",
                        "remaining": f"{rem:.1f}%",
                        "unit": "remaining",
                        "reset": reset_display,
                        "pace": "N/A",
                        "health": "good" if rem > 30 else "warning",
                        "detail": f"{name} [IDE/File]",
                        "data_source": "local_file",
                        "updated_at": datetime.now(UTC).isoformat(),
                        "provider_id": "antigravity",
                        "account_label": None,
                        "model_id": name,
                        "used_value": round(100.0 - rem, 4),
                        "limit_value": 100.0,
                        "unit_type": "percent",
                        "window_type": "session",
                        "reset_at": reset_at,
                    }
                )
            return res
        except Exception:
            return []
