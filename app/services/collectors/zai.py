"""
zAI (Zhipu AI/GLM) quota collector with prepaid balance tracking.

Collection Strategy:
- Requires ZAI_API_KEY environment variable (Zhipu API key)
- Calls https://open.bigmodel.cn/api/paas/v4/users/me/balance
- Returns prepaid account balance in Chinese Yuan (¥)
- Prepaid model: no quotas, just account balance

Error Handling:
- Missing/invalid keys: Returns error card with key validation message
- API errors (401, etc.): Returns error card with HTTP status
- Connection failures: Returns error card with generic message

Key Validation:
- Checks that key is not literally "zai" (placeholder detection)
"""

from typing import List, Dict, Any
import httpx
from app.core.config import settings
from app.core.utils import error_card
from app.services.collectors.base import BaseCollector


class ZaiCollector(BaseCollector):
    """Collector for zAI (Zhipu AI/GLM) prepaid balance."""
    
    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect zAI (Zhipu/GLM) prepaid balance.
        
        Requires ZAI_API_KEY. Validates key is not placeholder.
        Returns error card if key missing or API fails.
        
        Returns:
            List[Dict[str, Any]]: Single card with balance in ¥ or error
        """
        key = settings.ZAI_API_KEY
        if not key or key.lower() == "zai":
            return [error_card("zAI", "🌐", "Missing/Invalid Key")]
        
        try:
            resp = await client.get(
                "https://open.bigmodel.cn/api/paas/v4/users/me/balance",
                headers={"Authorization": f"Bearer {key}"}
            )
            
            if resp.status_code != 200:
                return [error_card("zAI", "🌐", f"API Error ({resp.status_code})")]
            
            data = resp.json()
            bal = float(data.get("data", {}).get("available_balance", 0))
            
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
        except httpx.RequestError:
            return [error_card("zAI", "🌐", "Connection Failed")]
        except (ValueError, KeyError, TypeError):
            return [error_card("zAI", "🌐", "Invalid Response")]
