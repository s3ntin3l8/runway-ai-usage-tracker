import json
from datetime import UTC, date, datetime

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.core.encryption import encryption_service
from app.models._datetime import UTCDateTime


class SidecarRegistry(SQLModel, table=True):
    """Persistent registry of known sidecars that have sent data."""

    __tablename__ = "sidecar_registry"

    sidecar_id: str = Field(primary_key=True)
    hostname: str | None = None  # socket.gethostname() from sidecar
    custom_name: str | None = None  # User-assigned display name
    tags_json: str | None = Field(default=None)  # JSON array stored as string
    last_seen: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    first_seen: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    last_ip: str | None = None
    error_count: int = Field(default=0)
    ingest_count: int = Field(default=0)
    sidecar_version: str | None = None  # App version reported by the sidecar
    os_platform: str | None = None  # OS/platform string (e.g. "Windows/10", "Darwin/24")
    # Whether the build can self-update in place (frozen, non-Docker). None =
    # not reported (legacy/permissive); False = from-source/Docker (no update push).
    self_update_capable: bool | None = None
    recent_logs: str | None = None  # JSON-encoded list of last log lines from the sidecar
    collection_enabled: bool = Field(
        default=True
    )  # False = sidecar paused, server skips poll instructions

    @property
    def tags(self) -> list[str]:
        return json.loads(self.tags_json) if self.tags_json else []

    @tags.setter
    def tags(self, value: list[str]):
        self.tags_json = json.dumps(value)


class WebhookConfig(SQLModel, table=True):
    """Per-provider webhook alert configuration."""

    __tablename__ = "webhook_configs"

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str  # provider name e.g. "anthropic", or "*" for global
    threshold_pct: float  # 0.0–100.0, e.g. 90.0
    url: str  # Discord or Slack incoming webhook URL
    channel: str  # "discord" or "slack" — validated by CRUD API at ingestion
    active: bool = Field(default=True)
    last_fired_at: UTCDateTime | None = Field(default=None)  # None = reset/ready to fire


class ProviderConfig(SQLModel, table=True):
    """Per-provider user configuration (API keys, labels, poll intervals, enabled toggle)."""

    __tablename__ = "provider_configs"
    __table_args__ = (UniqueConstraint("provider_id", "account_id", name="uq_provider_account"),)

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(index=True)
    account_id: str = Field(default="default")
    enabled: bool = Field(default=True)
    api_key_encrypted: str | None = Field(default=None)  # encrypted via encryption_service
    session_cookie_encrypted: str | None = Field(
        default=None
    )  # encrypted session/auth cookie override
    oai_sc_cookie_encrypted: str | None = Field(
        default=None
    )  # encrypted oai-sc service-credential cookie (ChatGPT)
    account_label: str | None = None
    poll_interval_seconds: int | None = None  # None = use collector default TTL
    collection_strategies_json: str | None = Field(default=None)  # JSON list of {id, enabled}

    @property
    def strategies(self) -> list[dict] | None:
        """Deserialize collected strategies config, or None if not set."""
        if not self.collection_strategies_json:
            return None
        return json.loads(self.collection_strategies_json)

    @strategies.setter
    def strategies(self, value: list[dict] | None) -> None:
        """Serialize strategy config to JSON string."""
        if value is not None:
            self.collection_strategies_json = json.dumps(value)
        else:
            self.collection_strategies_json = None

    @property
    def api_key(self) -> str | None:
        """Decrypt and return the stored API key, or None if not set."""
        if not self.api_key_encrypted:
            return None
        return encryption_service.decrypt_string(self.api_key_encrypted)

    @api_key.setter
    def api_key(self, value: str | None) -> None:
        """Encrypt and store the API key."""
        if value:
            self.api_key_encrypted = encryption_service.encrypt_string(value)
        else:
            self.api_key_encrypted = None

    @property
    def session_cookie(self) -> str | None:
        """Decrypt and return the stored session cookie, or None if not set."""
        if not self.session_cookie_encrypted:
            return None
        return encryption_service.decrypt_string(self.session_cookie_encrypted)

    @session_cookie.setter
    def session_cookie(self, value: str | None) -> None:
        """Encrypt and store the session cookie."""
        if value:
            self.session_cookie_encrypted = encryption_service.encrypt_string(value)
        else:
            self.session_cookie_encrypted = None

    @property
    def oai_sc_cookie(self) -> str | None:
        """Decrypt and return the stored oai-sc cookie, or None if not set."""
        if not self.oai_sc_cookie_encrypted:
            return None
        return encryption_service.decrypt_string(self.oai_sc_cookie_encrypted)

    @oai_sc_cookie.setter
    def oai_sc_cookie(self, value: str | None) -> None:
        """Encrypt and store the oai-sc cookie."""
        if value:
            self.oai_sc_cookie_encrypted = encryption_service.encrypt_string(value)
        else:
            self.oai_sc_cookie_encrypted = None


class SystemConfig(SQLModel, table=True):
    """Global application configuration (single row)."""

    __tablename__ = "system_config"

    id: int | None = Field(default=None, primary_key=True)
    browser_preference: str | None = None  # e.g. "safari,chrome,firefox"
    default_poll_interval_seconds: int | None = None  # None = use per-collector default TTL
    dashboard_layout_json: str | None = None
    user_timezone: str | None = (
        None  # IANA name, e.g. "Europe/Berlin"; None = use TZ env / browser detect
    )
    # Update channel sidecars track for the "update available" check: "stable"
    # (default) or "edge" (rolling prerelease). Pushed to sidecars via the
    # /fleet/ingest response. None = "stable".
    sidecar_update_channel: str | None = None
    # Fleet-wide opt-in: when true, sidecars self-install available updates.
    # Pushed via the /fleet/ingest response; a sidecar's explicit local
    # `auto_update` config overrides this. None/False = off.
    sidecar_auto_update: bool | None = None
    # Fernet key that signs browser session cookies, generated on first use
    # and stored encrypted-at-rest with DB_ENCRYPTION_KEY. Kept separate from
    # DB_ENCRYPTION_KEY so rotating it ("log out everywhere") invalidates all
    # sessions without re-encrypting provider secrets. See app/core/sessions.py.
    session_secret_encrypted: str | None = None


class LatestUsage(SQLModel, table=True):
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


class UsageEvent(SQLModel, table=True):
    """One assistant-message-level usage record. Source of truth.

    Idempotency: (provider_id, account_id, event_id) is UNIQUE — re-pushing
    the same log entry from a sidecar is a no-op.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "account_id",
            "event_id",
            name="uq_usage_events_identity",
        ),
        Index("ix_usage_events_account_ts", "provider_id", "account_id", "ts"),
        Index("ix_usage_events_account_model_ts", "provider_id", "account_id", "model_id", "ts"),
        Index("ix_usage_events_sidecar_ts", "sidecar_id", "ts"),
        Index("ix_usage_events_project_ts", "project", "ts"),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(index=True)  # "anthropic", "chatgpt", ...
    account_id: str = Field(index=True)  # canonical (email or hash)
    sidecar_id: str = Field(default="local")  # hostname that pushed this
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
    git_branch: str | None = None  # vcs branch at the time of the message, if logged
    tools_json: str | None = None  # JSON array of tool names used in the message (Anthropic)
    subagent_type: str | None = Field(
        default=None, index=True
    )  # "Explore" | "Plan" | None for main thread
    tokens_input: int = Field(default=0)  # non-cached prompt tokens
    tokens_output: int = Field(default=0)  # completion tokens
    tokens_cache_read: int = Field(default=0)  # free reads
    tokens_cache_create: int = Field(default=0)  # Anthropic cache writes (1.25x cost)
    tokens_reasoning: int = Field(default=0)  # o1-style thinking tokens
    cost_usd: float = Field(default=0.0)  # provider-reported or computed (authoritative total)
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
    ingested_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))


class UsageWindow(SQLModel, table=True):
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


class UsagePeriodRollup(SQLModel, table=True):
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


class QuotaSnapshot(SQLModel, table=True):
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


class ProviderPricing(SQLModel, table=True):
    """Per-model pricing in USD per million tokens.

    Time-versioned: a row's effective_from defines when it became active.
    For an event at time T, use the row with the largest effective_from <= T.
    """

    __tablename__ = "provider_pricing"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "model_id",
            "effective_from",
            name="uq_provider_pricing_identity",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(index=True)
    model_id: str = Field(index=True)
    effective_from: date
    input_per_mtok: float  # $/M input tokens (non-cached)
    output_per_mtok: float
    cache_read_per_mtok: float = Field(default=0.0)
    cache_create_per_mtok: float = Field(default=0.0)
    notes: str | None = None


class AuditLog(SQLModel, table=True):
    """Append-only record of admin mutations.

    Captures every successful state-changing call against the admin
    surface (sidecar pause/resume/delete/patch, plus future targets).
    Closes audit finding S7: logger.info-only records get rotated with
    the container, leaving no investigative trail when something
    surprising happens. Designed as an authoritative diagnostic, not a
    legal-grade trail — same trust model as the rest of Runway.
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_ts_desc", "ts"),)

    id: int | None = Field(default=None, primary_key=True)
    ts: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    # Who attempted the action. Resolved by require_admin_key:
    # "localhost", a proxy-asserted username, "api-key", or
    # "no-admin-key-configured" when ADMIN_API_KEY is unset.
    actor: str = Field(index=True)
    # Structured attribution alongside the human-readable `actor` string.
    # actor_type ∈ {"localhost","proxy","session","api-key","none"};
    # actor_meta_json holds proxy-supplied extras (email, groups) when known.
    # Nullable so existing rows (and the localhost/api-key cases) degrade
    # gracefully. Resolved by app/core/security.py:resolve_auth.
    actor_type: str | None = Field(default=None, index=True)
    actor_meta_json: str | None = None
    source_ip: str | None = None
    # Domain.verb, e.g. "sidecar.pause", "sidecar.update".
    action: str = Field(index=True)
    target_id: str | None = Field(default=None, index=True)
    # Optional structured detail (old vs new values, etc.); JSON-encoded.
    payload_json: str | None = None
