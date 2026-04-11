import logging
import httpx
from typing import List, Dict, Any, Optional
from app.services.collectors.base import BaseCollector
from app.core.config import settings
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class OpenRouterCollector(BaseCollector):
    """
    Collector for OpenRouter usage and credits.
    Uses: https://openrouter.ai/api/v1/credits
    """

    def __init__(self):
        self.api_key = settings.OPENROUTER_API_KEY

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect usage data from OpenRouter via API."""
        if not self.api_key:
            return []

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            
            # Check credits/usage
            resp = await client.get(
                "https://openrouter.ai/api/v1/credits",
                headers=headers,
                timeout=10
            )
            
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data:
                    info = data["data"]
                    total_credits = info.get("total_credits", 0.0)
                    usage = info.get("usage", 0.0)
                    remaining = max(0, total_credits - usage)
                    
                    return [{
                        "service": "OpenRouter Credits",
                        "icon": "🚀",
                        "remaining": f"${remaining:.2f}",
                        "unit": "USD",
                        "reset": "Prepaid",
                        "health": "good" if remaining > 5.0 else "warning" if remaining > 1.0 else "critical",
                        "pace": "Stable",
                        "detail": f"Used: ${usage:.2f} of ${total_credits:.2f} [API]",
                        "used_value": usage,
                        "limit_value": total_credits,
                        "unit_type": "currency",
                        "data_source": "api",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }]
            else:
                logger.error(f"OpenRouter API error (HTTP {resp.status_code}): {resp.text}")
                
        except Exception as e:
            logger.error(f"Failed to collect OpenRouter usage: {e}")

        return []

    def _fallback_strategies(self) -> List[Any]:
        """Return an ordered list of fallback async methods. Currently none for OpenRouter."""
        return []

    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return the ultimate error card(s) when all strategies fail."""
        from app.core.utils import error_card
        
        if not self.api_key:
            return [error_card("OpenRouter", "🚀", "Missing OPENROUTER_API_KEY", error_type="missing_config")]
            
        return [error_card("OpenRouter", "🚀", "API connection failed", error_type="api_error")]
