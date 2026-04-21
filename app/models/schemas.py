import html
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LimitCard(BaseModel):
    service_name: str
    icon: str = "❓"
    remaining: str = "—"
    unit: str = "units"
    reset: str = "—"
    health: str = "unknown"
    pace: str = "unknown"
    detail: str = ""
    # Raw values for consistent percentage calculations
    used_value: float | None = None
    limit_value: float | None = None
    is_unlimited: bool = False
    unit_type: str = "generic"  # "currency", "tokens", "requests", "minutes", "percent", "generic"
    currency: str | None = None  # "USD", "EUR", "CNY", etc.
    # ISO 8601 timestamp for hover tooltip with absolute time
    reset_at: str | None = None
    # Data collection mechanism (how)
    data_source: str = "unknown"  # "oauth", "web_api", "scrape", "logs", "statusline", "api"
    # Credential/Token origin (where)
    input_source: str = "unknown"  # "sidecar", "config", "server"
    # Error categorization
    error_type: str | None = None
    # Tier classification (None = no badge shown)
    tier: str | None = None  # "Free", "Pro", "Premium", "Team", "Enterprise"
    # URL to provider's usage/settings page
    usage_url: str | None = None
    # ISO 8601 timestamp when data was last collected/updated
    updated_at: str | None = None
    # Arbitrary metadata for internal transport (e.g. token extraction)
    metadata: dict[str, Any] | None = Field(default_factory=dict)

    # Phase 0B: Promoted schema fields
    provider_id: str | None = None  # e.g. "anthropic", "openai"
    account_id: str | None = None  # Unique account hash/ID
    account_label: str | None = None  # Human-readable: email, org name
    model_id: str | None = None  # None=aggregate snapshot; specific=per-model
    sidecar_id: str | None = None  # Originating host FQDN/tag; None=local
    window_type: str = "unknown"  # "daily","weekly","monthly","session","rolling","unknown"

    @field_validator("service_name", "remaining", "unit", "reset", "pace", "detail", "tier")
    @classmethod
    def escape_html_fields(cls, v: str) -> str:
        if v:
            return html.escape(v)
        return v

    model_config = ConfigDict(
        # Include None values in serialized output so frontend can check for tier field
        serialize_default_excluded=False  # type: ignore
    )


class LimitsResponse(BaseModel):
    limits: list[LimitCard]


class IngestRequest(BaseModel):
    provider: str
    metrics: list[LimitCard]
    sidecar_id: str | None = None  # Originating host identifier (Phase 4B fleet mgmt)
    sidecar_version: str | None = None  # App version from package.json
    os_platform: str | None = None  # platform.system() + "/" + platform.release()
    collection_errors: int = 0  # Number of provider collection failures in this cycle
    last_log_lines: list[str] = []  # Tail of sidecar log from the sending machine
    # api_key is now passed via X-Signature header for security
