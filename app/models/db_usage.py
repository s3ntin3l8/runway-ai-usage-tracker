from datetime import UTC, datetime

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.utc_datetime import UTCDateTime


class LatestUsage(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "latest_usage"
    __table_args__ = (
        # model_id must be in the identity tuple — collectors legitimately
        # emit multiple cards for the same (provider, account, window,
        # variant) tuple that differ only by model (e.g. Claude Sonnet
        # weekly vs Claude Opus weekly). sidecar_id is intentionally
        # excluded so that server-scraped rows and sidecar-enriched rows
        # for the same logical account merge into one row instead of
        # creating duplicates.
        UniqueConstraint(
            "provider_id",
            "account_id",
            "window_type",
            "variant",
            "model_id",
            name="uq_latest_usage_identity",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(index=True)
    account_id: str = Field(index=True)
    sidecar_id: str = Field(default="local", index=True)
    window_type: str = Field(default="unknown")
    variant: str = Field(default="default")
    model_id: str = Field(default="")
    card_json: str
    updated_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))


class LatestUsageContribution(SQLModel, table=True):  # type: ignore[call-arg]
    """Last successful card payload reported by one collection source.

    ``LatestUsage`` remains the merged read model; this table preserves source
    ownership so one complete report cannot retire another producer's card.
    """

    __tablename__ = "latest_usage_contributions"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "account_id",
            "source_id",
            "window_type",
            "variant",
            "model_id",
            name="uq_latest_usage_contribution_identity",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(index=True)
    account_id: str = Field(index=True)
    source_id: str = Field(index=True)
    window_type: str = Field(default="unknown")
    variant: str = Field(default="default")
    model_id: str = Field(default="")
    card_json: str
    updated_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))


class UsageEvent(SQLModel, table=True):  # type: ignore[call-arg]
    """One assistant-message-level usage record. Source of truth.

    Idempotency: (provider_id, event_id) is UNIQUE — re-pushing the same log
    entry from a sidecar is a no-op, and re-pushing it under a *different*
    account (the operator retagged the credential, a hint arrived) moves the
    existing row instead of counting it twice (see ``EventIngestor``). The
    older (provider_id, account_id, event_id) constraint is kept — it is
    implied by the new index and dropping it would mean rebuilding the table.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "account_id",
            "event_id",
            name="uq_usage_events_identity",
        ),
        Index("uq_usage_events_provider_event", "provider_id", "event_id", unique=True),
        Index("ix_usage_events_account_ts", "provider_id", "account_id", "ts"),
        Index("ix_usage_events_account_model_ts", "provider_id", "account_id", "model_id", "ts"),
        Index("ix_usage_events_sidecar_ts", "sidecar_id", "ts"),
        # Covers query_cumulative_live's WHERE kind='message' AND ts >= :since —
        # the identity-agnostic scans (Home's month-only cumulative, /fleet's
        # sidecar contributions) filter only on these two columns.
        Index("ix_usage_events_kind_ts", "kind", "ts"),
        Index("ix_usage_events_project_ts", "project", "ts"),
        # Covers query_source_apps' WHERE kind='message' AND source_app IS NOT
        # NULL AND ts >= :since scan (the per-app breakdown, which is
        # identity-agnostic unless a provider/account filter narrows it).
        Index("ix_usage_events_source_app_ts", "source_app", "ts"),
        Index(
            "ix_usage_events_account_session_ts", "provider_id", "account_id", "session_id", "ts"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(index=True)  # "anthropic", "chatgpt", ...
    account_id: str = Field(index=True)  # canonical (email or hash)
    attribution_source: str = "unknown"  # local | tag | default | unknown
    credential_origin: str | None = None  # directory-scoped events; None for legacy streams
    sidecar_id: str = Field(default="local")  # hostname that pushed this
    # The client app that produced the event ("opencode", "claude_code",
    # "minimax_code", ...). provider_id names the upstream provider billed, which
    # several client apps share, so a canonical-provider retag (see
    # scripts/sidecar_pkg/canonical_providers.py) would erase where the traffic
    # came from. source_app keeps that fact. Falls back to provider_id when the
    # pushing sidecar doesn't report one.
    source_app: str | None = None
    event_id: str  # provider's msg_id / request_id
    ts: UTCDateTime = Field(index=True)  # actual log timestamp (UTC)
    kind: str = Field(
        default="message", index=True
    )  # "message" | "error" | reserved: "reset", "anomaly"
    model_id: str | None = None  # normalized: opus-4.8, sonnet-4.5, gpt-5, ...
    session_id: str | None = None  # provider's conversation/session id
    # Working-directory / project context (enrichment; sidecar-provided). cwd is
    # the raw per-message path; project is the session's root basename
    # (server-derived in EventIngestor via app/services/project_label.py, then
    # consolidated per-session offline) and indexed for the Top Projects ranking +
    # the per-session project filter.
    cwd: str | None = None
    project: str | None = Field(default=None, index=True)
    # The git repository the working directory belongs to, as the name only.
    # The client resolves it on its own host and reports the name; the server
    # never reads the client's filesystem. NULL when the client could not
    # resolve one (no git, not a repository, an older sidecar). A maintenance
    # repair may also write it: the `cloud_session_lead_repo` check applies the
    # repository a cloud session's own labelled events name most often.
    project_ref: str | None = None
    git_branch: str | None = None  # vcs branch at the time of the message, if logged
    tools_json: str | None = None  # JSON array of tool names used in the message (Anthropic)
    subagent_type: str | None = Field(
        default=None, index=True
    )  # "Explore" | "Plan" | None for main thread
    # The provider's own turn identifier, when it has one (MiniMax Code).
    # A provider that emits one event per request uses it to count turns
    # instead of requests; NULL means "one event per message".
    turn_id: str | None = None
    tokens_input: int = Field(default=0)  # non-cached prompt tokens
    tokens_output: int = Field(default=0)  # completion tokens
    tokens_cache_read: int = Field(default=0)  # free reads
    tokens_cache_create: int = Field(default=0)  # Anthropic cache writes, sum of 1h+5m below
    # Split of tokens_cache_create by TTL (1h writes cost 2x base input, 5m cost
    # 1.25x — see cost_calculator.compute_event_cost_breakdown). Both 0 means the
    # split is unknown (pre-fix backfill or a non-Anthropic provider); the total
    # is then priced as all-5m, matching the historical single-rate behavior.
    tokens_cache_create_1h: int = Field(default=0)
    tokens_cache_create_5m: int = Field(default=0)
    tokens_reasoning: int = Field(default=0)  # o1-style thinking tokens
    cost_usd: float = Field(default=0.0)  # provider-reported or computed (authoritative total)
    cost_reported_usd: float | None = None  # raw provider/tool-reported cost, when present
    cost_estimated_usd: float = Field(default=0.0)  # token value at configured API rates
    # USD cost components (sum ≈ cost_usd; reasoning billed at the output rate folds
    # into cost_output). Stored so any cost-composition view (e.g. exclude-cache) needs
    # no recompute; cost_usd stays authoritative when a provider supplies its own total.
    cost_input: float = Field(default=0.0)
    cost_output: float = Field(default=0.0)
    cost_cache_read: float = Field(default=0.0)
    cost_cache_create: float = Field(default=0.0)
    stop_reason: str | None = None  # end_turn, max_tokens, tool_use, error
    tool_calls: int = Field(default=0)  # number of tool_use blocks
    latency_ms: int | None = None  # request duration if logged
    raw_json: str | None = None  # original log line for debugging
    raw_provider_id: str | None = None  # OpenCode's raw providerID (gateway tag)
    # Claude Code per-message dimensions (Anthropic JSONL; None/0 elsewhere).
    effort: str | None = None  # "low" | "medium" | "high" | "xhigh" | "max"
    speed: str | None = None  # "fast" when fast mode was active, else "standard"
    service_tier: str | None = None  # "standard" | "priority" | "batch"
    entrypoint: str | None = None  # "cli" | "sdk-ts" | "claude-vscode" | ...
    app_version: str | None = None  # Claude Code version that logged the message
    web_search_requests: int = Field(default=0)
    web_fetch_requests: int = Field(default=0)
    ingested_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))


class UsageWindow(SQLModel, table=True):  # type: ignore[call-arg]
    """One row per (window_type, model_id × sidecar_id) combination per closed window.

    Written exactly once when a window's authoritative reset_at advances past
    its end. Captures the final totals for that window — never updated.
    """

    __tablename__ = "usage_windows"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "account_id",
            "window_type",
            "window_end",
            "series_model_id",
            "series_variant",
            "model_id",
            "sidecar_id",
            name="uq_usage_windows_identity",
        ),
        Index("ix_usage_windows_history", "provider_id", "account_id", "window_type", "window_end"),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str
    account_id: str
    window_type: str  # session, daily, weekly, monthly, weekly_sonnet, ...
    window_start: UTCDateTime
    window_end: UTCDateTime  # the reset_at from authoritative scrape
    # Quota-card identity (distinct from the model/sidecar event breakdown below).
    # Empty strings mark legacy rows whose originating quota series is unknown.
    series_model_id: str = Field(default="")
    series_variant: str = Field(default="")
    model_id: str = Field(default="")  # "" = all-models rollup row
    sidecar_id: str = Field(default="")  # "" = all-sidecars rollup row
    msgs: int = Field(default=0)
    tokens_input: int = Field(default=0)
    tokens_output: int = Field(default=0)
    tokens_cache_read: int = Field(default=0)
    tokens_cache_create: int = Field(default=0)
    tokens_reasoning: int = Field(default=0)
    cost_usd: float = Field(default=0.0)
    limit_value: float | None = None
    pct_used: float | None = None


class UsagePeriodRollup(SQLModel, table=True):  # type: ignore[call-arg]
    """Pre-aggregated period totals for fast dashboard reads.

    Maintained incrementally: every UsageEvent insert updates the matching
    rows for day, month, year, and lifetime — at the (model_id, sidecar_id)
    grain plus the all-up rollups.
    """

    __tablename__ = "usage_period_rollup"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "account_id",
            "period_type",
            "period_key",
            "model_id",
            "sidecar_id",
            name="uq_usage_period_rollup_identity",
        ),
        Index(
            "ix_usage_period_rollup_lookup",
            "provider_id",
            "account_id",
            "period_type",
            "period_key",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str
    account_id: str
    period_type: str  # hour, day, month, year, lifetime
    period_key: str  # 2026-05-08T14, 2026-05-08, 2026-05, 2026, 'all'
    model_id: str = Field(default="")
    sidecar_id: str = Field(default="")
    msgs: int = Field(default=0)
    tokens_input: int = Field(default=0)
    tokens_output: int = Field(default=0)
    tokens_cache_read: int = Field(default=0)
    tokens_cache_create: int = Field(default=0)
    tokens_reasoning: int = Field(default=0)
    cost_usd: float = Field(default=0.0)
    # Per-component cost sums, mirroring UsageEvent (see there).
    cost_input: float = Field(default=0.0)
    cost_output: float = Field(default=0.0)
    cost_cache_read: float = Field(default=0.0)
    cost_cache_create: float = Field(default=0.0)
    last_updated: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))


class QuotaSnapshot(SQLModel, table=True):  # type: ignore[call-arg]
    """Append-only time series of scraped quota observations.

    Written on every upsert_latest_usage call when pct_used is non-null.
    Provides intra-window fill history for the % used chart.
    """

    __tablename__ = "quota_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "account_id",
            "window_type",
            "variant",
            "model_id",
            "ts",
            name="uq_quota_snapshots_identity",
        ),
        Index("ix_quota_snapshots_lookup", "provider_id", "account_id", "window_type", "ts"),
        # Covers the bucketed window-function queries in query_snapshots /
        # query_chart percent path: PARTITION BY (provider, account,
        # window_type, variant, model_id) ORDER BY ts. See app/core/db.py for
        # the idempotent CREATE INDEX migration applied at startup.
        Index(
            "ix_quota_snapshots_series_ts",
            "provider_id",
            "account_id",
            "window_type",
            "variant",
            "model_id",
            "ts",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str
    account_id: str
    window_type: str  # weekly, daily, session, monthly
    variant: str = Field(default="")  # "" = default variant
    model_id: str = Field(default="")  # "" = all-models aggregate
    ts: UTCDateTime = Field(index=True)
    pct_used: float | None = None
    reset_at: UTCDateTime | None = None


class PendingUsageEvent(SQLModel, table=True):  # type: ignore[call-arg]
    """A collected event awaiting evidence-backed or manual account assignment."""

    __tablename__ = "pending_usage_events"
    __table_args__ = (
        UniqueConstraint("provider_id", "event_id", "sidecar_id", name="uq_pending_usage_event"),
        Index("ix_pending_usage_events_provider_ts", "provider_id", "ts"),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str
    event_id: str
    sidecar_id: str
    ts: UTCDateTime = Field(index=True)
    reason: str = "account_unresolved"
    payload_json: str
    first_seen: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
