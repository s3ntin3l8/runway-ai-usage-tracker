from typing import List, Optional, Dict, Any
from pydantic import BaseModel, ConfigDict, field_validator, Field
import html


class LimitCard(BaseModel):
    service_name: str
    icon: str
    remaining: str
    unit: str
    reset: str
    health: str
    pace: str
    detail: str
    # Raw values for consistent percentage calculations
    used_value: Optional[float] = None
    limit_value: Optional[float] = None
    is_unlimited: bool = False
    unit_type: str = (
        "generic"  # "currency", "tokens", "requests", "minutes", "percent", "generic"
    )
    currency: Optional[str] = None  # "USD", "EUR", "CNY", etc.
    # ISO 8601 timestamp for hover tooltip with absolute time
    reset_at: Optional[str] = None
    # Data source indicator for display in UI
    data_source: str = (
        "unknown"  # "oauth", "web_api", "local", "cache", "fallback", "api", "sidecar"
    )
    # Error categorization
    error_type: Optional[str] = None
    # Tier classification (None = no badge shown)
    tier: Optional[str] = None  # "Free", "Pro", "Premium", "Team", "Enterprise"
    # URL to provider's usage/settings page
    usage_url: Optional[str] = None
    # ISO 8601 timestamp when data was last collected/updated
    updated_at: Optional[str] = None
    # Arbitrary metadata for internal transport (e.g. token extraction)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)

    # Phase 0B: Promoted schema fields
    provider_id: Optional[str] = None        # e.g. "anthropic", "openai"
    account_id: Optional[str] = None         # Unique account hash/ID
    account_label: Optional[str] = None      # Human-readable: email, org name
    model_id: Optional[str] = None           # None=aggregate snapshot; specific=per-model
    sidecar_id: Optional[str] = None         # Originating host FQDN/tag; None=local
    window_type: str = "unknown"             # "daily","weekly","monthly","session","rolling","unknown"

    @field_validator("service_name", "remaining", "unit", "reset", "pace", "detail", "tier")
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
    sidecar_id: Optional[str] = None  # Originating host identifier (Phase 4B fleet mgmt)
    # api_key is now passed via X-Signature header for security
