"""
Chinese AI provider quota collectors (zAI/GLM and Kimi K2.5).

Collection Strategy:
1. zAI (GLM - Zhipu AI)
   - Requires ZAI_API_KEY environment variable (Zhipu API key)
   - Calls https://open.bigmodel.cn/api/paas/v4/users/me/balance
   - Returns prepaid account balance in Chinese Yuan (¥)
   - Prepaid model: no quotas, just account balance
   
2. Kimi K2.5 (Moonshot AI)
   - Requires KIMI_API_KEY environment variable (Moonshot API key)
   - Calls https://api.moonshot.cn/v1/users/me/balance
   - Returns prepaid account balance in USD ($)
   - Prepaid model: no quotas, just account balance

Error Handling:
- Missing/invalid keys: Returns error card with key validation message
- API errors (401, etc.): Returns error card with HTTP status
- Connection failures: Returns error card with generic message

Key Validation:
- zAI: Checks that key is not literally "zai" (placeholder)
- Kimi: Checks that key length >= 10 (minimum valid key length)

Balance Thresholds:
- zAI: Warning if balance < ¥10
- Kimi: Warning if balance < $5
"""

from typing import List, Dict, Any
import httpx
from app.core.config import settings
from app.core.utils import error_card
from app.services.collectors.base import BaseCollector

class ChineseAICollector(BaseCollector):
    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect balance from zAI and Kimi Chinese AI providers.
        
        Independently queries each provider and returns available cards.
        Returns empty list if both fail or keys missing.
        
        Returns:
            List[Dict[str, Any]]: Cards for each available provider
        """
        results = []
        
        # 1. zAI (GLM)
        zai_res = await self._get_zai(client)
        if zai_res: results.extend(zai_res)
        
        # 2. Kimi
        kimi_res = await self._get_kimi(client)
        if kimi_res: results.extend(kimi_res)
        
        return results

    async def _get_zai(self, client: httpx.AsyncClient):
        """
        Fetch zAI (Zhipu/GLM) prepaid balance.
        
        Requires ZAI_API_KEY. Validates key is not placeholder.
        Returns error card if key missing or API fails.
        
        Returns:
            List[Dict[str, Any]]: Single card with balance in ¥ or error
        """
        key = settings.ZAI_API_KEY
        if not key or "zai" in key: return [error_card("zAI", "🌐", "Missing/Invalid Key")]
        try:
            resp = await client.get("https://open.bigmodel.cn/api/paas/v4/users/me/balance", headers={"Authorization": f"Bearer {key}"})
            if resp.status_code != 200: return [error_card("zAI", "🌐", "API Error")]
            bal = float(resp.json().get("data", {}).get("available_balance", 0))
            return [{
                "service": "zAI (GLM)",
                "icon": "🌐",
                "remaining": f"¥{bal:.2f}",
                "unit": "balance",
                "reset": "Manual",
                "health": "good" if bal > 10 else "warning",
                "pace": "Stable",
                "detail": "Prepaid balance",
            }]
        except: return [error_card("zAI", "🌐", "Connection Failed")]

    async def _get_kimi(self, client: httpx.AsyncClient):
        """
        Fetch Kimi K2.5 (Moonshot AI) prepaid balance.
        
        Requires KIMI_API_KEY (Moonshot API key with length >= 10).
        Handles 401 Unauthorized separately to distinguish auth issues.
        Returns error card if key missing or API fails.
        
        Returns:
            List[Dict[str, Any]]: Single card with balance in $ or error
        """
        key = settings.KIMI_API_KEY
        if not key or len(key) < 10: return [error_card("Kimi K2.5", "🌙", "Missing/Invalid Key")]
        try:
            resp = await client.get("https://api.moonshot.cn/v1/users/me/balance", headers={"Authorization": f"Bearer {key}"})
            if resp.status_code == 401: return [error_card("Kimi K2.5", "🌙", "Unauthorized")]
            if resp.status_code != 200: return [error_card("Kimi K2.5", "🌙", f"HTTP {resp.status_code}")]
            bal = float(resp.json().get("data", {}).get("available_balance", 0))
            return [{
                "service": "Kimi K2.5",
                "icon": "🌙",
                "remaining": f"${bal:.2f}",
                "unit": "balance",
                "reset": "Manual",
                "health": "good" if bal > 5 else "warning",
                "pace": "Stable",
                "detail": "Prepaid balance",
            }]
        except: return [error_card("Kimi K2.5", "🌙", "Connection Failed")]
