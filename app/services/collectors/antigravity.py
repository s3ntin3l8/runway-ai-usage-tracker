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

import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import httpx
from app.core.config import settings
from app.core.utils import PaceCalculator, human_delta
from app.services.collectors.base import BaseCollector

class AntigravityCollector(BaseCollector):
    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect Antigravity IDE quota from local JSON file.
        
        Reads ANTIGRAVITY_QUOTA_PATH and returns cards for each model.
        Silently fails if file unavailable (assumes IDE not configured).
        
        Returns:
            List[Dict[str, Any]]: Cards for each model or empty list if unavailable
        """
        path = settings.ANTIGRAVITY_QUOTA_PATH
        try:
            with open(path, "r") as f: data = json.load(f)
            res = []
            for name, usage in data.get("models", {}).items():
                rem = usage.get("remaining_percent", 0.0)
                reset = datetime.fromtimestamp(usage["resets_at"], tz=timezone.utc) if "resets_at" in usage else None
                res.append({
                    "service": f"AG: {name}",
                    "icon": "🛸",
                    "remaining": f"{rem:.1f}%",
                    "unit": "remaining",
                    "reset": human_delta(reset),
                    "health": "good" if rem > 30 else "warning",
                    "pace": PaceCalculator.estimate_longevity(100 - rem, reset),
                    "detail": f"{name} [IDE]",
                    "reset_at": reset.isoformat() if reset else None,
                    "data_source": "local",
                })
            return res
        except (FileNotFoundError, PermissionError, json.JSONDecodeError, KeyError, ValueError):
            return []
