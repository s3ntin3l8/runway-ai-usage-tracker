import logging
import httpx
from typing import List, Dict, Any, Optional
from app.services.collectors.base import BaseCollector
from app.core.config import settings
from app.services.token_cache import token_cache
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class OpenRouterCollector(BaseCollector):
    """
    Collector for OpenRouter usage and credits.
    Uses: https://openrouter.ai/api/v1/credits
    """

    PROVIDER_ID = "openrouter"
    DEFAULT_WINDOW_TYPE = "monthly"

    def __init__(self, account_id: Optional[str] = None, account_label: Optional[str] = None):
        super().__init__(account_id=account_id, account_label=account_label)

    async def _get_api_key(self) -> Optional[str]:
        """Discovery API key from cache or settings."""
        # 1. Try account-specific token from cache
        if self.account_id:
            token = await token_cache.get_token("openrouter", "api_key", account_id=self.account_id)
            if token:
                return token
        
        # 2. Fallback to settings
        return settings.OPENROUTER_API_KEY

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect usage data from OpenRouter via API."""
        api_key = await self._get_api_key()
        if not api_key:
            return []

        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
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
                        "service_name": "OpenRouter Credits",
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
        
        api_key = await self._get_api_key()
        if not api_key:
            return [error_card("OpenRouter", "🚀", "Missing OPENROUTER_API_KEY", error_type="missing_config")]
            
        return [error_card("OpenRouter", "🚀", "API connection failed", error_type="api_error")]
