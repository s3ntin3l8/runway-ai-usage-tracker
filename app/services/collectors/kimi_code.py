"""
Kimi Code (Moonshot AI) quota collector with prepaid balance tracking.

Collection Strategy:
- Requires KIMI_API_KEY environment variable (Moonshot API key)
- Calls https://api.moonshot.cn/v1/users/me/balance
- Returns prepaid account balance in USD ($)
- Prepaid model: no quotas, just account balance

Error Handling:
- Missing/invalid keys: Returns error card with key validation message
- API errors (401, etc.): Returns error card with HTTP status
- Connection failures: Returns error card with generic message

Key Validation:
- Checks that key length >= 10 (minimum valid key length)
"""

from typing import List, Dict, Any
import httpx
from app.core.config import settings
from app.core.utils import error_card
from app.services.collectors.base import BaseCollector


class KimiCodeCollector(BaseCollector):
    """Collector for Kimi Code (Moonshot AI) prepaid balance."""
    
    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect Kimi Code (Moonshot AI) prepaid balance.
        
        Requires KIMI_API_KEY (Moonshot API key with length >= 10).
        Handles 401 Unauthorized separately to distinguish auth issues.
        Returns error card if key missing or API fails.
        
        Returns:
            List[Dict[str, Any]]: Single card with balance in $ or error
        """
        key = settings.KIMI_API_KEY
        if not key or len(key) < 10:
            return [error_card("Kimi Code", "🌙", "Missing/Invalid Key")]
        
        try:
            resp = await client.get(
                "https://api.moonshot.cn/v1/users/me/balance",
                headers={"Authorization": f"Bearer {key}"}
            )
            
            if resp.status_code == 401:
                return [error_card("Kimi Code", "🌙", "Unauthorized")]
            if resp.status_code != 200:
                return [error_card("Kimi Code", "🌙", f"HTTP {resp.status_code}")]
            
            data = resp.json()
            bal = float(data.get("data", {}).get("available_balance", 0))
            
            return [{
                "service": "Kimi Code",
                "icon": "🌙",
                "remaining": f"${bal:.2f}",
                "unit": "balance",
                "reset": "Manual",
                "health": "good" if bal > 5 else "warning",
                "pace": "Stable",
                "detail": "Prepaid balance",
            }]
        except httpx.RequestError:
            return [error_card("Kimi Code", "🌙", "Connection Failed")]
        except (ValueError, KeyError, TypeError):
            return [error_card("Kimi Code", "🌙", "Invalid Response")]
