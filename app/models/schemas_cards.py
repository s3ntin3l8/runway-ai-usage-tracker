import html
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreditBalanceComponent(BaseModel):
    """One purchased or promotional tranche explaining an upstream balance."""

    kind: Literal["purchased", "subscription", "promotion"]
    label: str
    currency: str
    remaining_value: float
    granted_value: float | None = None
    expires_at: str | None = None
    products: list[str] = Field(default_factory=list)


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
    credit_breakdown: list[CreditBalanceComponent] | None = None
    # Cloud models the free usage allowance applies to (Ollama settings page)
    free_models: list[str] | None = None
    # ISO 8601 timestamp for hover tooltip with absolute time
    reset_at: str | None = None
    # Data collection mechanism (how)
    data_source: str = "unknown"  # "api", "web", "local", "unknown", "error"
    # Credential/Token origin (where)
    input_source: str = "unknown"  # "sidecar", "config", "server"
    # Error categorization
    error_type: str | None = None
    # Tier classification (None = no badge shown)
    tier: str | None = None  # "Free", "Pro", "Premium", "Team", "Enterprise"
    # What kind of evidence this card is (None = it can decide the billing model)
    billing_evidence: str | None = None  # "placeholder" | "prepaid_credit"
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
    # Set by SmartCollector when cache age exceeds STALE_CEILING_SECONDS.
    # Signals collection failure — the card data is real but old. The frontend
    # uses this to skip the health→critical override so stale cards don't
    # appear in the at-risk rail unless quota is genuinely near the limit.
    stale: bool = False
    # Set alongside `stale` when SmartCollector serves past-STALE_CEILING
    # cache because collection is failing, and by the startup residual scrub
    # for pre-#293 rows. Every current writer sets it together with `stale`
    # (and the merge recovery pops both) — cardStale() still ORs them so the
    # structured field alone is sufficient once the detail-regex fallback is
    # removed (#312). The frontend prefers this field over parsing the detail
    # display string.
    collection_failing: bool = False

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
