from typing import List, Optional, Dict, Any
from pydantic import BaseModel, ConfigDict, field_validator, Field
import html

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
    # NEW: Error categorization
    error_type: Optional[str] = None
    # NEW: Tier classification (None = no badge shown)
    tier: Optional[str] = None  # "Free", "Pro", "Premium", "Team", "Enterprise"
    # NEW: URL to provider's usage/settings page
    usage_url: Optional[str] = None
    # NEW: ISO 8601 timestamp when data was last collected/updated
    updated_at: Optional[str] = None
    # NEW: Flexible metadata for structured data (tokens, raw metrics)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("service", "remaining", "unit", "reset", "pace", "detail", "tier")
    @classmethod
    def escape_html_fields(cls, v: str) -> str:
        if v:
            return html.escape(v)
        return v
    
    model_config = ConfigDict(
        # Include None values in serialized output so frontend can check for tier field
        serialize_default_excluded=False
    )

class LimitsResponse(BaseModel):
    limits: List[LimitCard]

class IngestRequest(BaseModel):
    provider: str
    metrics: List[LimitCard]
    # api_key is now passed via X-Signature header for security
