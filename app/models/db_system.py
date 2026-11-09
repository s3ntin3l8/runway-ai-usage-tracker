from datetime import UTC, date, datetime

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.utc_datetime import UTCDateTime


class WebhookConfig(SQLModel, table=True):  # type: ignore[call-arg]
    """Per-provider/per-account webhook alert configuration."""

    __tablename__ = "webhook_configs"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "account_id",
            "url",
            name="uq_webhook_provider_account_url",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str  # provider name e.g. "anthropic", or "*" for global
    account_id: str | None = Field(default=None)  # None = applies to all accounts
    threshold_pct: float  # 0.0–100.0, e.g. 90.0
    url: str  # Discord or Slack incoming webhook URL
    channel: str  # "discord" or "slack" — validated by CRUD API at ingestion
    active: bool = Field(default=True)
    last_fired_at: UTCDateTime | None = Field(default=None)  # None = reset/ready to fire
    # Also alert when a matching credential's Token Health status turns
    # expired/invalid — see app.services.credential_alerts. Default true so
    # existing (threshold-only) webhooks pick it up without an opt-in step.
    credential_alerts: bool = Field(default=True)


class WebhookCredentialAlert(SQLModel, table=True):  # type: ignore[call-arg]
    """Dedup/re-arm state for one webhook's credential-health alerts.

    A row means *webhook_id* has already alerted for *provider_id*/*account_id*'s
    current bad (expired/invalid) episode — deleting it re-arms the alert.
    `account_id` is already the canonical, resolved id (see
    `account_identity.resolve_account_id`), matching how webhook account
    scoping is compared elsewhere.

    `healthy_since` implements hysteresis: Token Health's `invalid` status
    comes from the in-memory `auth_failures` registry, which any successful
    collect clears, so a single healthy observation is not proof the
    credential is fixed. `app.services.credential_alerts` only deletes the
    row (re-arming) once `healthy_since` is old enough.
    """

    __tablename__ = "webhook_credential_alerts"
    __table_args__ = (
        UniqueConstraint(
            "webhook_id",
            "provider_id",
            "account_id",
            name="uq_webhook_credential_alert_scope",
        ),
        Index("ix_webhook_credential_alerts_webhook", "webhook_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    webhook_id: int
    provider_id: str
    account_id: str
    status: str  # "failing" | "expired" | "invalid" — the worst status seen this episode
    fired_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    healthy_since: UTCDateTime | None = Field(default=None)


class SystemConfig(SQLModel, table=True):  # type: ignore[call-arg]
    """Global application configuration (single row)."""

    __tablename__ = "system_config"

    id: int | None = Field(default=None, primary_key=True)
    credential_file_migration_json: str | None = None
    browser_preference: str | None = None  # e.g. "safari,chrome,firefox"
    default_poll_interval_seconds: int | None = None  # None = use per-collector default TTL
    dashboard_layout_json: str | None = None
    # JSON list of extra worktree directory patterns for project labels
    # (see app/services/project_label.py); None = built-in containers only.
    worktree_patterns_json: str | None = None
    user_timezone: str | None = (
        None  # IANA name, e.g. "Europe/Berlin"; None = use TZ env / browser detect
    )
    # Update channel sidecars track for the "update available" check: "stable"
    # (default), "beta" (numbered prereleases), or "edge" (rolling build).
    # Pushed to sidecars via /fleet/ingest. None = "stable".
    sidecar_update_channel: str | None = None
    # Fleet-wide opt-in: when true, sidecars self-install available updates.
    # Pushed via the /fleet/ingest response; a sidecar's explicit local
    # `auto_update` config overrides this. None/False = off.
    sidecar_auto_update: bool | None = None
    # Fleet-wide keep-alive default: when true, every sidecar renews its CLI logins unless its
    # own Fleet override says otherwise. It can only turn keep-alive on — it never switches off
    # a sidecar whose local flag is on. Pushed via the /fleet/ingest response. None/False = off.
    sidecar_keep_alive_default: bool | None = None
    # Claude cloud-runner collector switch set in the app. None = use the
    # CLAUDE_CLOUD_COLLECTOR_ENABLED environment default.
    claude_cloud_collector_enabled: bool | None = None
    # Fernet key that signs browser session cookies, generated on first use
    # and stored encrypted-at-rest with DB_ENCRYPTION_KEY. Kept separate from
    # DB_ENCRYPTION_KEY so rotating it ("log out everywhere") invalidates all
    # sessions without re-encrypting provider secrets. See app/core/sessions.py.
    session_secret_encrypted: str | None = None


class ProviderPricing(SQLModel, table=True):  # type: ignore[call-arg]
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
    cache_create_per_mtok: float = Field(
        default=0.0
    )  # 5-minute TTL cache writes (Anthropic: 1.25x)
    # 1-hour TTL cache writes (Anthropic: 2x input). 0.0 = no dedicated rate, so
    # compute_event_cost_breakdown falls back to cache_create_per_mtok for any
    # 1h-flagged tokens too (matches pre-split behavior for other providers).
    cache_create_1h_per_mtok: float = Field(default=0.0)
    # Optional long-prompt tier (Haiku 5.5): when an event's prompt size
    # (input + cache read + cache create tokens) is GREATER than the threshold,
    # the whole event bills at the long_* rates. NULL threshold = no tier.
    long_prompt_threshold_tokens: int | None = None
    long_input_per_mtok: float | None = None
    long_output_per_mtok: float | None = None
    long_cache_read_per_mtok: float | None = None
    long_cache_create_per_mtok: float | None = None
    long_cache_create_1h_per_mtok: float | None = None
    # Optional time-of-day schedule (see app/services/pricing_schedule.py): while
    # the event's UTC ts is inside the named schedule's window every rate above
    # is multiplied by rate_schedule_multiplier. NULL = flat rates.
    rate_schedule: str | None = None
    rate_schedule_multiplier: float | None = None
    notes: str | None = None


class AuditLog(SQLModel, table=True):  # type: ignore[call-arg]
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


class SessionName(SQLModel, table=True):  # type: ignore[call-arg]
    """A human-readable name for a session, one row per (provider, session, kind).

    ``kind`` is ``agent`` (set by the user or a launcher) or ``generated``
    (produced by the tool). Not a column on ``usage_events``: a name changes
    during a session, and one event row per name would repeat it. The text is
    free-form and may hold sensitive wording, so it is never logged.
    """

    __tablename__ = "session_names"
    __table_args__ = (
        # Its autoindex also serves the (provider_id, session_id) page lookup.
        UniqueConstraint("provider_id", "session_id", "kind", name="uq_session_names_key"),
    )
    id: int | None = Field(default=None, primary_key=True)
    provider_id: str
    session_id: str
    source_app: str | None = None
    name: str
    kind: str
    updated_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
