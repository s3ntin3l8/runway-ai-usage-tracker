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
    # Token usage breakdown from usage page scraping
    token_usage: dict[str, Any] | None = (
        None  # {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "total": 0}
    )
    by_model: dict[str, Any] | None = (
        None  # {"model-name": {"cost": float, "msgs": int, "tokens": dict}}
    )
    msgs: int | None = None  # Total message count
    pct_used: float | None = None  # Percentage used (based on cost vs limit)

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


class ForecastEntry(BaseModel):
    provider_id: str
    account_id: str | None
    account_label: str | None
    model_id: str | None
    service_name: str
    window_type: str
    unit_type: str
    now_used: float | None
    now_pct: float | None
    projected_used: float | None
    projected_pct: float | None
    projected_limit_hit_at: str | None = None
    limit_value: float
    reset_at: str  # ISO-8601 UTC (copied from card)
    window_start: str  # ISO-8601 UTC
    samples_used: int
    confidence: float  # 0.0–1.0
    status: str  # "ok" | "warn" | "risk" | "insufficient_data" | "stable" | "exhausted"
    method: str  # "linear" for now


class ForecastResponse(BaseModel):
    forecasts: list[ForecastEntry]
    summary: dict[
        str, int
    ]  # {"risk": n, "warn": n, "ok": n, "insufficient_data": n, "stable": n, "exhausted": n}
    generated_at: str  # ISO-8601 UTC
