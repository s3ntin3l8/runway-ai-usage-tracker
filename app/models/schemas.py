from typing import List, Optional
from pydantic import BaseModel

class LimitCard(BaseModel):
    service: str
    icon: str
    remaining: str
    unit: str
    reset: str
    health: str
    pace: str
    detail: str
    # NEW: Raw values for consistent percentage calculations
    used_value: Optional[float] = None
    limit_value: Optional[float] = None
    is_unlimited: bool = False
    unit_type: str = "generic"  # "currency", "tokens", "requests", "minutes", "percent", "generic"
    currency: Optional[str] = None  # "USD", "EUR", "CNY", etc.
    # NEW: ISO 8601 timestamp for hover tooltip with absolute time
    reset_at: Optional[str] = None
    # NEW: Data source indicator for display in UI
    data_source: str = "unknown"  # "oauth", "web_api", "local", "cache", "fallback", "api", "sidecar"

class LimitsResponse(BaseModel):
    limits: List[LimitCard]

class IngestRequest(BaseModel):
    provider: str
    metrics: List[LimitCard]
    api_key: str
