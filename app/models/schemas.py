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
    window_type: str = "unknown"  # "session","daily","weekly","monthly","rolling","unknown"
    variant: str | None = (
        None  # per-card disambiguator under same (provider, account, model_id, window_type)
    )
    # Explicit signal that this card shares a physical quota with other cards
    # carrying the same non-null value. Set by collectors that know multiple
    # model rows draw from one bucket (e.g. Antigravity's per-model session
    # cards all reference the same session limit). Cards with `None` stand
    # alone — the dashboard never clusters them by behavioral similarity.
    # Convention: f"{provider_id}:{window_type}:{reset_at_iso}".
    quota_pool_id: str | None = None
    # Token usage breakdown from usage page scraping
    token_usage: dict[str, Any] | None = (
        None  # {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "total": 0}
    )
    by_model: dict[str, Any] | None = (
        None  # {"model-name": {"cost": float, "msgs": int, "tokens": dict}}
    )
    msgs: int | None = None  # Total message count
    pct_used: float | None = None  # Percentage used (based on cost vs limit)

    @field_validator(
        "service_name", "remaining", "unit", "reset", "pace", "detail", "tier", "variant"
    )
    @classmethod
    def escape_html_fields(cls, v: str) -> str:
        if v:
            return html.escape(v)
        return v

    model_config = ConfigDict(
        # Include None values in serialized output so frontend can check for tier field
        serialize_default_excluded=False,  # type: ignore
        # Allow extra fields so transport metadata like _enrichment_detail aren't stripped
        extra="allow",
    )


class LimitsResponse(BaseModel):
    limits: list[LimitCard]


class UsageEventPush(BaseModel):
    """One usage event pushed by a sidecar."""

    provider_id: str
    account_id: str
    event_id: str  # dedup key
    ts: str  # ISO-8601 (UTC) of the actual event
    model_id: str | None = None
    session_id: str | None = None
    subagent_type: str | None = None  # "Explore" | "Plan" | None for main thread
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_create: int = 0
    tokens_reasoning: int = 0
    stop_reason: str | None = None
    tool_calls: int = 0
    latency_ms: int | None = None
    raw_json: str | None = None
    # cost_usd: set by providers that log it directly (e.g. OpenCode); None = server computes
    # sidecar_id intentionally NOT here — comes from IngestRequest.sidecar_id
    cost_usd: float | None = None
    kind: str = "message"  # "message" | "error"; reserved: "reset", "anomaly"
    error_reason: str | None = (
        None  # short tag: "rate_limit", "auth_failed", "quota_exceeded", "timeout"
    )


class IngestRequest(BaseModel):
    """Updated body for /api/v1/fleet/ingest."""

    provider: str
    metrics: list[LimitCard] = Field(default_factory=list)
    events: list[UsageEventPush] = Field(default_factory=list)  # NEW
    sidecar_id: str | None = None  # Originating host identifier (Phase 4B fleet mgmt)
    sidecar_version: str | None = None  # App version from package.json
    os_platform: str | None = None  # platform.system() + "/" + platform.release()
    # Whether this build can self-update in place (frozen, non-Docker). None for
    # sidecars that don't report it yet → server stays permissive.
    self_update_capable: bool | None = None
    collection_errors: int = 0  # Number of provider collection failures in this cycle
    last_log_lines: list[str] = Field(default_factory=list)
    # api_key is now passed via X-Signature header for security
    # NOTE: deltas field removed — replaced by events[]


class ForecastEntry(BaseModel):
    provider_id: str
    account_id: str | None
    account_label: str | None
    model_id: str | None
    service_name: str
    window_type: str
    variant: str | None = None
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
    # "ok" | "warn" | "risk" | "insufficient_data" | "stable" | "exhausted"
    # | "decelerating" | "low_resolution" | "near_limit"
    status: str
    method: str  # "linear" | "theil_sen"
    # Pct per second from the trend fit; null when no fit. Lets the UI render
    # trend arrows without re-inferring from projected vs current.
    slope: float | None = None
    # Glide-path target: where the card SHOULD be if usage were paced evenly
    # across the window. = clamp(elapsed/total_window, 0, 1) × 100. Same numeric
    # value as `confidence × 100`, but exposed separately because the metrics
    # carry different semantics — confidence = "trust in the fit", glide_pct =
    # "expected pace position". Matches dashboard fleet-commander.js formula.
    glide_pct: float | None = None
    # Optional drill-down payload populated only when the endpoint is called
    # with ?include_series=true. Each item is {"ts": iso, "pct": float}.
    series: list[dict[str, float | str]] | None = None


class ForecastResponse(BaseModel):
    forecasts: list[ForecastEntry]
    # Keyed by status: risk, warn, ok, insufficient_data, stable, exhausted,
    # decelerating, low_resolution, near_limit
    summary: dict[str, int]
    generated_at: str  # ISO-8601 UTC


class TopModelEntry(BaseModel):
    """One model's totals in the cross-provider Top Models ranking."""

    model_id: str
    msgs: int
    tokens_total: int
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_create: int
    tokens_reasoning: int
    cost_usd: float
    cost_cache: float  # cache_read + cache_create cost, for exclude-cache
    providers: list[str]  # distinct providers that contributed this model


class TopModelsResponse(BaseModel):
    models: list[TopModelEntry]
    metric: str  # "tokens" | "cost" — the sort key used
    generated_at: str  # ISO-8601 UTC


class GlobalLifetimeTotals(BaseModel):
    tokens_total: int
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_create: int
    tokens_reasoning: int
    tokens_cache: int  # cache_read + cache_create
    cost_usd: float
    cost_cache: float
    msgs: int


class GlobalSessionStats(BaseModel):
    count: int
    avg_cost: float
    avg_tokens: float


class GlobalBusiestDay(BaseModel):
    period_key: str  # "YYYY-MM-DD" (UTC calendar date)
    tokens: int


class GlobalBusiestHour(BaseModel):
    hour: int  # 0–23 in the user's local timezone
    tokens: int


class GlobalStatsResponse(BaseModel):
    lifetime: GlobalLifetimeTotals
    sessions: GlobalSessionStats
    cache_hit_ratio: float  # cache_read / all tokens, 0..1
    distinct_models: int
    distinct_providers: int
    busiest_day: GlobalBusiestDay | None
    busiest_hour: GlobalBusiestHour | None
    generated_at: str  # ISO-8601 UTC
