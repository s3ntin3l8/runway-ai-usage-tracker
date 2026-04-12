import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import httpx
import asyncio
from app.core.utils import PaceCalculator, human_delta, http_request_with_retry
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

class ChatGPTApiMixin:
    """Mixin for ChatGPT Web API collection."""
    
    async def _fetch_api_data(
        self, client: httpx.AsyncClient, token: str, account_id: Optional[str], source: str
    ) -> List[Dict[str, Any]]:
        """Fetch from ChatGPT backend."""
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "oai-device-id": await self._get_device_id(),
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id

        now = datetime.now(timezone.utc)
        usage_resp = await http_request_with_retry(client, "GET", "https://chatgpt.com/backend-api/wham/usage", headers=headers, timeout=10)
        
        if usage_resp.status_code == 200:
            data = usage_resp.json()
            tier = data.get("plan_type", "free")
            email = data.get("email", "")
            
            # Identity Promotion
            if email and self.account_id:
                asyncio.create_task(token_cache.update_account_metadata("chatgpt", self.account_id, name=email))
                self.account_label = email

            primary = data.get("rate_limit", {}).get("primary_window", {})
            if primary:
                pct = primary.get("used_percent", 0.0)
                reset_ts = primary.get("reset_at")
                reset_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc) if reset_ts else None

                return [{
                    "service_name": "ChatGPT Codex",
                    "icon": "💬",
                    "remaining": f"{(100-pct):.1f}%",
                    "unit": "remaining",
                    "reset": human_delta(reset_at),
                    "health": "good" if pct < 80 else "warning",
                    "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                    "detail": f"{tier.upper()} Account · {email} · {pct:.1f}% used",
                    "used_value": float(pct),
                    "limit_value": 100.0,
                    "unit_type": "percent",
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": source,
                    "tier": tier,
                    "updated_at": now.isoformat(),
                }]
        return []
