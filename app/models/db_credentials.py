import json
from datetime import UTC, datetime

from sqlalchemy import Index, UniqueConstraint, text
from sqlmodel import Field, SQLModel

from app.core.encryption import encryption_service
from app.models._datetime import UTCDateTime


class ProviderConfig(SQLModel, table=True):  # type: ignore[call-arg]
    """Per-provider user configuration (API keys, labels, poll intervals, enabled toggle)."""

    __tablename__ = "provider_configs"
    __table_args__ = (UniqueConstraint("provider_id", "account_id", name="uq_provider_account"),)

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(index=True)
    account_id: str = Field(default="default")
    enabled: bool = Field(default=True)
    archived: bool = Field(default=False)
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
    opencode_workspace_id: str | None = Field(default=None)
    claude_organization_id: str | None = Field(default=None)
    # How monetary values should be presented for this account.
    billing_type: str = "unknown"  # subscription | pay_as_you_go | unknown
    # Who wrote ``billing_type``: "operator" when a person chose it in
    # Settings, "detected" when the server proved it, and ``None`` when
    # nobody decided (the value is the default). It is what tells a
    # deliberate choice apart from a row that was never detected, so a
    # backfill never overwrites what the operator picked.
    billing_type_source: str | None = None  # operator | detected | None

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


class ProviderAccountLabel(SQLModel, table=True):  # type: ignore[call-arg]
    """Operator label override for discovered identities without config rows."""

    __tablename__ = "provider_account_labels"
    __table_args__ = (
        UniqueConstraint("provider_id", "account_id", name="uq_provider_account_label"),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(index=True)
    account_id: str = Field(index=True)
    account_label: str | None = None


class ProviderAccountMerge(SQLModel, table=True):  # type: ignore[call-arg]
    """A durable account merge: the source account went to the destination.

    The machine keeps resolving its credential to the source id, so the
    ingest needs to know that the id was retired by a merge — and where it
    went. Without this the source account comes back on the next push.
    """

    __tablename__ = "provider_account_merges"
    __table_args__ = (
        UniqueConstraint("provider_id", "source_account_id", name="uq_provider_account_merge"),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(index=True)
    source_account_id: str = Field(index=True)
    destination_account_id: str = Field(index=True)
    merged_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    # What this merge moved, as JSON text — the rows it re-filed (by id), the
    # credential sources, tags and contributions it carried over, and what
    # happened to the source's provider_configs row. NULL for a merge that
    # predates the journal: such a merge cannot be reversed.
    moved: str | None = None


class CredentialSource(SQLModel, table=True):  # type: ignore[call-arg]
    """Durable, non-secret settings and health for one account credential source."""

    __tablename__ = "credential_sources"
    __table_args__ = (
        UniqueConstraint("provider_id", "account_id", "source_id", name="uq_credential_source"),
        Index("ix_credential_sources_account", "provider_id", "account_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str
    account_id: str
    source_id: str
    source_type: str  # config | sidecar | env | file | server
    source_label: str
    credential_origin: str | None = None
    sidecar_id: str | None = None
    credential_expires_at: UTCDateTime | None = None
    token_types_json: str | None = None
    enabled: bool = Field(default=True)
    priority: int = Field(default=0)
    last_seen: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    # healthy | auth_failed | unavailable | degraded. "untried" is derived at read time by
    # ``credential_sources.effective_health`` and never stored.
    health: str = Field(default="healthy")
    health_detail: str | None = None
    # Collection provenance, stamped by the failover loop: which source actually
    # produced data, and when. "Active source" for an account = latest last_success_at.
    last_attempt_at: UTCDateTime | None = None
    last_success_at: UTCDateTime | None = None
    last_error: str | None = None
    # The stable, non-email id a cookie collector last saw behind this source (a Kimi JWT
    # ``sub``), and the account it was recorded under. A changed subject for the same account
    # means the browser behind the cookie switched users.
    verified_subject: str | None = None
    verified_subject_account: str | None = None
    # Consecutive failed attempts (auth_failed / unavailable); any success resets it. An
    # auth failure also pushes ``next_retry_at`` out on the verification backoff schedule so
    # a revoked credential isn't re-tried at the head of every cycle.
    consecutive_failures: int = 0
    next_retry_at: UTCDateTime | None = None
    # When the current failure streak began (cleared by a success): a provider outage must
    # last a while before every source it touches reads ``failing``.
    failing_since: UTCDateTime | None = None


class CredentialTag(SQLModel, table=True):  # type: ignore[call-arg]
    """Operator-set mapping from a host-side credential origin to a server-side identity.

    Silent-listener storage. The sidecar reads ``/fleet/config`` to
    discover tag hints (one per ``credential_origin``); if no tag exists,
    the matching credential card is blocked from the ingest queue and the
    sidecar reports the origin via ``POST /fleet/credentials/manifest`` so
    the operator can resolve it in the fleet UI. Resolutions persist here
    keyed on ``(provider_id, credential_origin, sidecar_id)``. See PR #288.

    ``credential_origin`` is a stable host-side descriptor like
    ``"path:/home/user/.claude/.credentials.json"`` or ``"env:ANTHROPIC_API_KEY"`` —
    never a credential value, never a hash of one.

    ``sidecar_id`` scopes the tag: ``NULL`` = deployment-wide (legacy
    rows and the dialog's "All machines" scope — two sidecars sharing
    the same origin, e.g. an NFS-shared home dir, intentionally resolve
    to one tag, since the credential itself is genuinely one identity);
    a concrete id = that sidecar only (the dialog's default — hosts that
    hold the same logical origin but carry different per-host
    credentials). Reads resolve scoped-first, then fall back to the
    deployment-wide row. Partial unique indexes keep both forms
    collision-free (SQLite treats NULLs as distinct, so the split is
    required — see ``_migrate_credential_tag_scoping`` in
    ``app/core/db.py``). Tracking issue: #319.
    """

    __tablename__ = "credential_tags"
    __table_args__ = (
        Index(
            "uq_credential_tag_deployment",
            "provider_id",
            "credential_origin",
            unique=True,
            sqlite_where=text("sidecar_id IS NULL"),
        ),
        Index(
            "uq_credential_tag_sidecar",
            "provider_id",
            "credential_origin",
            "sidecar_id",
            unique=True,
            sqlite_where=text("sidecar_id IS NOT NULL"),
        ),
        Index("ix_credential_tags_provider", "provider_id"),
        Index("ix_credential_tags_sidecar", "sidecar_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    # Indexed via the explicit ``ix_credential_tags_*`` entries above —
    # no ``index=True`` here, which would add a second, identical index.
    provider_id: str
    credential_origin: str
    account_id: str  # matches provider_configs.account_id
    # NULL = deployment-wide; otherwise the sidecar this tag is scoped to.
    sidecar_id: str | None = Field(default=None)
    set_by: str = "operator"
    set_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    # Provider that owns ``account_id`` when it differs from ``provider_id``
    # (e.g. Gemini usage assigned to an Antigravity account). NULL = same
    # provider; events are rewritten to this provider at ingest.
    target_provider_id: str | None = Field(default=None)


class PendingCredentialTag(SQLModel, table=True):  # type: ignore[call-arg]
    """Origin the sidecar is currently reporting but no operator tag exists yet.

    Maintained by ``POST /fleet/credentials/manifest``: the sidecar sends
    the set of origins it found this cycle, the server upserts each row,
    then deletes any row for that ``sidecar_id`` whose origin is absent
    from the new list (silent disappearance of a local credential).

    Powers the "Untagged credentials" surface on the fleet view: the
    webapp reads these via ``GET /fleet/credentials/tags/pending`` and
    offers a tag action that creates a :class:`CredentialTag` and deletes
    the pending row.

    The pending state is keyed per-sidecar so the operator can answer
    "which host is this credential from?" before resolving, and so the
    auto-hint heuristic can scope ``provider:<pid>`` hints to the hosts
    that actually reported the credential (#319). A resolved
    :class:`CredentialTag` may itself be sidecar-scoped or
    deployment-wide — see that model's ``sidecar_id`` column.
    """

    __tablename__ = "pending_credential_tags"
    __table_args__ = (
        UniqueConstraint(
            "sidecar_id",
            "provider_id",
            "credential_origin",
            name="uq_pending_credential_tag_identity",
        ),
        Index("ix_pending_credential_tags_sidecar", "sidecar_id"),
        Index(
            "ix_pending_credential_tags_unresolved",
            "provider_id",
            "credential_origin",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    sidecar_id: str  # indexed via ``ix_pending_credential_tags_sidecar``
    provider_id: str
    credential_origin: str
    claimed_account_id: str | None = None
    first_seen: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    # Safe quota fields only; tokens and cookies are never written here.
    claude_organizations_json: str | None = None
    claude_organizations_complete: bool = False
    quota_preview_json: str | None = None
    quota_preview_observed_at: UTCDateTime | None = None
    # Identity-verifier retry state: a source that cannot name its account is retried
    # with exponential backoff instead of on every poll. Cleared when it is promoted
    # (the row is deleted) or when ingest stores a different secret.
    verify_attempts: int = 0
    next_verify_at: UTCDateTime | None = None
    # Why the sidecar reported this origin: "token_withheld" = the token stayed on the machine
    # (no server-side verifier for the provider), so quota will not collect until an account is
    # assigned. "source_locked" / "source_unsupported" / "source_unreadable" = a credential
    # source the sidecar found but could not read (a browser cookie store the browser holds
    # open, or one whose values are app-bound encrypted). None = an older sidecar, or an
    # origin that is only awaiting verification.
    reason: str | None = None
    # The unusable source behind a "source_*" reason: which store, why it could not be read,
    # and what the operator can do about it. Never a credential value, an account id or a
    # filesystem path.
    source_store: str | None = None
    source_detail: str | None = None
    source_remedy: str | None = None
