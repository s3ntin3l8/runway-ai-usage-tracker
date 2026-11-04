from pydantic import BaseModel


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
    cost_input: float = 0.0
    cost_output: float = 0.0
    cost_cache_read: float = 0.0
    cost_cache_create: float = 0.0
    providers: list[str]  # distinct providers that contributed this model


class TopModelsResponse(BaseModel):
    models: list[TopModelEntry]
    metric: str  # "tokens" | "cost" — the sort key used
    total: int  # the untruncated number of ranked groups
    limit: int  # the cap that was applied
    generated_at: str  # ISO-8601 UTC


class SourceAppEntry(BaseModel):
    """One client app's totals in the per-app usage breakdown."""

    source_app: str
    msgs: int
    tokens_total: int
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_create: int
    tokens_reasoning: int
    cost_usd: float
    cost_cache: float  # cache_read + cache_create cost, for exclude-cache
    cost_input: float = 0.0
    cost_output: float = 0.0
    cost_cache_read: float = 0.0
    cost_cache_create: float = 0.0
    providers: list[str]  # distinct providers this app's usage was billed to


class SourceAppsResponse(BaseModel):
    apps: list[SourceAppEntry]
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


class TopProjectEntry(BaseModel):
    """One project's totals in the Top Projects ranking."""

    project: str
    msgs: int
    sessions: int
    tokens_total: int
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_create: int
    tokens_reasoning: int
    cost_usd: float
    cost_cache: float
    cost_input: float = 0.0
    cost_output: float = 0.0
    cost_cache_read: float = 0.0
    cost_cache_create: float = 0.0
    providers: list[str]  # distinct providers that touched this project


class TopProjectsResponse(BaseModel):
    projects: list[TopProjectEntry]
    metric: str  # "tokens" | "cost" | "sessions"
    total: int  # the untruncated number of ranked groups
    limit: int  # the cap that was applied
    generated_at: str


class TopSessionEntry(BaseModel):
    """One session's totals in the cross-provider Top Sessions ranking."""

    session_id: str
    project: str | None = None
    cwd: str | None = None
    sidecar_id: str | None = None  # the host that pushed the session's latest event
    ts_start: str | None = None  # ISO-8601 UTC
    ts_end: str | None = None  # ISO-8601 UTC
    hosts: int = 0  # distinct hosts the session ran on
    msgs: int
    tokens_total: int
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_create: int
    tokens_reasoning: int
    cost_usd: float
    cost_cache: float
    cost_input: float = 0.0
    cost_output: float = 0.0
    cost_cache_read: float = 0.0
    cost_cache_create: float = 0.0
    providers: list[str]  # distinct providers that contributed to this session
    models: list[str]  # distinct models used in this session


class TopSessionsResponse(BaseModel):
    sessions: list[TopSessionEntry]
    metric: str  # "tokens" | "cost"
    total: int  # the untruncated number of ranked groups
    limit: int  # the cap that was applied
    generated_at: str


class TopHostEntry(BaseModel):
    """One host's totals in the cross-provider Top Hosts ranking."""

    sidecar_id: str  # the hostname that pushed these events
    ts_start: str | None = None  # ISO-8601 UTC
    ts_end: str | None = None  # ISO-8601 UTC
    msgs: int
    sessions: int  # distinct sessions that ran on this host
    tokens_total: int
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_create: int
    tokens_reasoning: int
    cost_usd: float
    cost_cache: float
    cost_input: float = 0.0
    cost_output: float = 0.0
    cost_cache_read: float = 0.0
    cost_cache_create: float = 0.0
    providers: list[str]  # distinct providers that pushed to this host


class TopHostsResponse(BaseModel):
    hosts: list[TopHostEntry]
    metric: str  # "tokens" | "cost"
    total: int  # the untruncated number of ranked groups
    limit: int  # the cap that was applied
    generated_at: str


class TopToolEntry(BaseModel):
    tool: str
    calls: int  # total invocations
    msgs: int  # distinct messages that used it


class TopToolsResponse(BaseModel):
    tools: list[TopToolEntry]
    total: int  # the untruncated number of ranked groups
    limit: int  # the cap that was applied
    generated_at: str
