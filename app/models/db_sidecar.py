import json
from datetime import UTC, datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.utc_datetime import UTCDateTime


class SidecarRequestReceipt(SQLModel, table=True):  # type: ignore[call-arg]
    """Short-lived digest receipts; authoritative authentication state."""

    __tablename__ = "sidecar_request_receipts"
    receipt_id: str = Field(primary_key=True)
    expires_at: float = Field(index=True)


class SidecarDevice(SQLModel, table=True):  # type: ignore[call-arg]
    """Paired device identity; the hostname is never an authorization boundary."""

    __tablename__ = "sidecar_devices"
    device_id: str = Field(primary_key=True)
    hostname: str | None = None
    platform: str | None = None  # Bound at pairing; ingest cannot change consent applicability.
    secret_encrypted: str
    revoked: bool = False
    created_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    # Last accepted ``POST /fleet/source-sync`` from this device. Ingest also touches the
    # source-state rows, so only a real sync request may set it (#1050).
    last_source_sync_at: UTCDateTime | None = None


class SidecarSourceState(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "sidecar_source_states"
    __table_args__ = (UniqueConstraint("device_id", "source_id", name="uq_device_scraper"),)
    id: int | None = Field(default=None, primary_key=True)
    device_id: str = Field(index=True)
    source_id: str
    provider_id: str
    label: str
    revision: str
    generation: int = 0
    revoked_through: int = -1
    local_read: bool = False
    remote_paused: bool = False
    report_json: str = "{}"
    dismissed_revision: str | None = None
    last_report_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))


class SidecarSourceContribution(SQLModel, table=True):  # type: ignore[call-arg]
    """Encrypted self-contained credential bundle, never a heartbeat lease."""

    __tablename__ = "sidecar_source_contributions"
    __table_args__ = (
        UniqueConstraint(
            "device_id", "provider_id", "credential_source_id", name="uq_device_credential"
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    device_id: str = Field(index=True)
    scraper_id: str | None = None
    provider_id: str
    credential_source_id: str
    origin: str
    account_id: str
    generation: int = 0
    payload_encrypted: str | None = None


class SidecarSourceCommand(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "sidecar_source_commands"
    id: int | None = Field(default=None, primary_key=True)
    device_id: str = Field(index=True)
    source_id: str
    generation: int
    action: str
    acknowledged: str | None = None
    issued_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))


class SidecarSourceRevision(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "sidecar_source_revision"
    id: int = Field(default=1, primary_key=True)
    revision: int = 0


class SidecarRegistry(SQLModel, table=True):  # type: ignore[call-arg]
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
    # Whether the sidecar runs with --keep-alive (renews the KEEP_ALIVE_PROVIDERS logins itself).
    # None = not reported (older sidecar) — shown as "unknown", never as "off".
    keep_alive: bool | None = None
    # Operator's remote setting for it, delivered on every ingest response. None = no
    # preference: the sidecar's own flag/config decides.
    keep_alive_desired: bool | None = None
    # Per-provider detail, JSON ``{provider_id: bool}``. ``keep_alive_providers`` is what the
    # sidecar reports it runs per login (None = a sidecar too old to report it);
    # ``keep_alive_desired_providers`` holds the operator's per-login overrides, which sit on top of
    # the sidecar-level ``keep_alive_desired`` / the sidecar's own flag. Only overridden logins
    # have an entry.
    keep_alive_providers: str | None = None
    keep_alive_desired_providers: str | None = None
    # Auto-update and update channel. ``*_desired`` is the operator's per-sidecar override
    # (None = follow the fleet-wide SystemConfig value); the plain fields are what the sidecar
    # reports it effectively uses (None = an older sidecar that doesn't report them), which
    # differs when its own config.json / RUNWAY_UPDATE_CHANNEL beats the dashboard.
    auto_update: bool | None = None
    auto_update_desired: bool | None = None
    update_channel: str | None = None
    update_channel_desired: str | None = None
    # Log level. ``log_level_desired`` is the operator's override, raised from the dashboard while
    # debugging (None = the sidecar's own config.json level); ``log_level`` is what the sidecar
    # reports it logs at (None = an older sidecar that doesn't report it).
    log_level: str | None = None
    log_level_desired: str | None = None
    login_dirs_desired: str | None = None  # JSON Fleet additions; never expanded on the server
    login_dirs_report: str | None = None  # JSON acknowledgement and searched directories
    recent_logs: str | None = None  # JSON-encoded list of last log lines from the sidecar
    # JSON: provider_id → {"account_id", "source"} from the latest check-in.
    identity_sources: str | None = None
    collection_enabled: bool = Field(
        default=True
    )  # False = sidecar paused, server skips poll instructions
    # One-shot admin "Update now" push, persisted (not in-memory) so it survives
    # a server restart and is reliably delivered on the sidecar's next
    # successful ingest — see fleet_registry.set_pending_update/consume_pending_update.
    pending_update: bool = Field(default=False)

    @property
    def tags(self) -> list[str]:
        return json.loads(self.tags_json) if self.tags_json else []

    @tags.setter
    def tags(self, value: list[str]):
        self.tags_json = json.dumps(value)


class SidecarPairingCode(SQLModel, table=True):  # type: ignore[call-arg]
    """One-time codes that let a fresh sidecar fetch its server URL + ingest key.

    Minted by an admin on the Fleet page (``POST /fleet/pairing-codes``) and
    carried to the sidecar via a ``runway-sidecar://pair`` deep link or typed
    in by hand; redeemed once over TLS by ``POST /fleet/pair``. Only the SHA-256
    of the code is stored, codes expire after a few minutes, and a redeemed
    row is kept (``used_at``) for the audit trail until the next sweep.
    """

    __tablename__ = "sidecar_pairing_codes"

    id: int | None = Field(default=None, primary_key=True)
    code_hash: str = Field(index=True, unique=True)
    # Canonical server URL the sidecar should talk to (what the admin's
    # browser saw, or PUBLIC_URL) — returned on redeem as the new api_url.
    server_url: str
    created_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: UTCDateTime = Field(index=True)
    created_by: str | None = None
    used_at: UTCDateTime | None = None
    used_by_hostname: str | None = None
