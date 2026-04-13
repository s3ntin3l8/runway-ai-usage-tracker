import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.services.collectors.base import BaseCollector

logger = logging.getLogger(__name__)

class MiniMaxCollector(BaseCollector):
    """
    Collector for MiniMax (Coding Plan) usage.
    Uses: https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains
    """

    PROVIDER_ID = "minimax"
    DEFAULT_WINDOW_TYPE = "monthly"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)
        self.api_key = settings.MINIMAX_API_KEY

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect usage data from MiniMax via API."""
        if not self.api_key:
            return []

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            
            resp = await client.get(
                "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains",
                headers=headers,
                timeout=10
            )
            
            if resp.status_code == 200:
                data = resp.json()
                results = []
                now_str = datetime.now(UTC).isoformat()
                
                model_remains = data.get("model_remains", [])
                for item in model_remains:
                    name = item.get("model_name", "Unknown")
                    remains = item.get("remains", 0)
                    
                    results.append({
                        "service_name": f"MiniMax: {name}",
                        "icon": "🤖",
                        "remaining": f"{remains:,}",
                        "unit": "requests",
                        "reset": "Coding Plan",
                        "health": "good" if remains > 100 else "warning" if remains > 20 else "critical",
                        "pace": "Active",
                        "detail": f"{name} quota [API]",
                        "used_value": 0.0, # MiniMax API returns remains
                        "limit_value": float(remains), # Approximation
                        "unit_type": "count",
                        "data_source": "api",
                        "updated_at": now_str,
                    })
                return results
            logger.error(f"MiniMax API error (HTTP {resp.status_code}): {resp.text}")
                
        except Exception as e:
            logger.error(f"Failed to collect MiniMax usage: {e}")

        return []

    def _fallback_strategies(self) -> list[Any]:
        """Return an ordered list of fallback async methods. Currently none for MiniMax."""
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return the ultimate error card(s) when all strategies fail."""
        from app.core.utils import error_card
        
        if not self.api_key:
            return [error_card("MiniMax", "🤖", "Missing MINIMAX_API_KEY", error_type="missing_config")]
            
        return [error_card("MiniMax", "🤖", "API connection failed", error_type="api_error")]
